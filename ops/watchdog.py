#!/usr/bin/env python3
"""Watchdog local d'Holaguia — supervision & auto-guérison (OPS-3).

Incident du 01/09 : holaguia.com injoignable ~36 min pour le propriétaire, et à
chaud personne ne savait si le serveur était tombé ou si c'était la box. La leçon,
indépendante de la cause : **voir de l'extérieur**, et **guérir seul** quand c'est
bien le serveur.

Ce script, déclenché chaque minute par un timer systemd (patron
`casaguide-send-guides`), sonde `/health` PAR CADDY (`https://holaguia.com/health`,
le chemin réel du client) avec un délai court. Après `THRESHOLD` (3) échecs
CONSÉCUTIFS → `systemctl restart casaguide` (et si Caddy lui-même ne répond pas
localement → `restart caddy`), journal en clair, **email au propriétaire** (mailer
Infomaniak existant), puis **cooldown** de 10 min pour ne jamais boucler. Le
compteur d'échecs revient à zéro au premier succès.

Le cœur (`decide`) est PUR (aucune E/S) ; l'orchestration (`run_once`) reçoit sonde
et actions par INJECTION → entièrement testable sans réseau ni systemd. Le compteur
d'échecs consécutifs survit d'une minute à l'autre dans un petit fichier d'état
(`/run/…`, tmpfs — remis à zéro au reboot, ce qui est correct).

Lancement (timer systemd chaque minute, ou à la main sur le serveur en root) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/watchdog.py

Charge `backend/.env` lui-même (hors EnvironmentFile systemd, OPS-1).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))          # ops/ (opsenv)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))  # api.*
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.watchdog")

# ── Réglages (surchageables par l'environnement) ─────────────────────────────
THRESHOLD = int(os.getenv("CASAGUIDE_WATCHDOG_THRESHOLD", "3"))   # échecs avant action
COOLDOWN_S = int(os.getenv("CASAGUIDE_WATCHDOG_COOLDOWN_S", "600"))  # 10 min anti-boucle
PROBE_TIMEOUT_S = float(os.getenv("CASAGUIDE_WATCHDOG_TIMEOUT_S", "8"))
HEALTH_URL = os.getenv("CASAGUIDE_WATCHDOG_URL", "https://holaguia.com/health")
CADDY_LOCAL_URL = os.getenv("CASAGUIDE_WATCHDOG_CADDY_URL", "http://127.0.0.1:80/")
STATE_FILE = os.getenv("CASAGUIDE_WATCHDOG_STATE", "/run/casaguide-watchdog.json")


# ── Cœur PUR ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Probe:
    """Résultat d'une sonde : `ok` + `detail` humain en cas d'échec (« 503 base »,
    « injoignable (timeout) »…) pour le journal et l'email."""
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Decision:
    """Verdict PUR d'un tick : quoi redémarrer (`None`/`"casaguide"`/`"caddy"`),
    l'état à persister, et le message humain (vide si aucune action)."""
    restart: str | None
    state: dict
    reason: str


def decide(state: dict, *, public: Probe, caddy_ok: bool, now: float,
           threshold: int = THRESHOLD, cooldown_s: int = COOLDOWN_S) -> Decision:
    """Décision PURE d'un tick à partir de l'état précédent et des sondes.

    Règles : succès → compteur à zéro (jamais d'action). Échec → +1 ; sous le seuil,
    on attend. Au seuil, si un redémarrage a eu lieu il y a moins de `cooldown_s`,
    on NE reboucle PAS (compteur conservé). Sinon on redémarre : **caddy** si Caddy
    ne répond pas localement, **casaguide** sinon (le gel de l'app est le cas le plus
    fréquent) ; le compteur repart à zéro et l'instant du redémarrage est mémorisé."""
    last_restart = state.get("last_restart")
    if public.ok:
        return Decision(None, {"failures": 0, "last_restart": last_restart}, "")

    failures = int(state.get("failures", 0)) + 1
    pending = {"failures": failures, "last_restart": last_restart}
    if failures < threshold:
        return Decision(None, pending, "")               # pas encore le seuil
    if last_restart is not None and now - last_restart < cooldown_s:
        return Decision(None, pending, "")               # cooldown : ne jamais boucler

    if not caddy_ok:
        service, cause = "caddy", "Caddy ne répond pas localement"
    else:
        service, cause = "casaguide", (public.detail or "échec /health")
    reason = (f"{failures} échecs consécutifs /health ({cause}) "
              f"→ {service} redémarré")
    return Decision(service, {"failures": 0, "last_restart": now}, reason)


def run_once(state: dict, *, probe_public: Callable[[], Probe],
             probe_caddy: Callable[[], Probe], restart: Callable[[str], None],
             notify: Callable[[str, float], None], now: float,
             threshold: int = THRESHOLD, cooldown_s: int = COOLDOWN_S) -> dict:
    """Un tick complet, sonde et actions INJECTÉES (testable sans réseau ni systemd).
    Renvoie le nouvel état à persister. Ne sonde Caddy localement que si le public
    échoue (économie). Sur décision de redémarrage : journal humain → restart → email.
    Un échec d'email ne bloque JAMAIS l'auto-guérison (best-effort)."""
    public = probe_public()
    caddy_ok = True if public.ok else probe_caddy().ok
    decision = decide(state, public=public, caddy_ok=caddy_ok, now=now,
                      threshold=threshold, cooldown_s=cooldown_s)
    if decision.restart:
        log.warning("watchdog : %s", decision.reason)
        restart(decision.restart)
        try:
            notify(decision.reason, now)
        except Exception as exc:  # noqa: BLE001 — l'alerte ne doit jamais bloquer la guérison
            log.warning("watchdog : alerte email non envoyée (%s)", exc)
    elif not public.ok:
        log.info("watchdog : /health en échec (%s) — %d/%d",
                 public.detail, int(decision.state.get("failures", 0)), threshold)
    return decision.state


# ── Persistance de l'état (compteur inter-minutes) ───────────────────────────

def load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"failures": 0, "last_restart": None}


def save_state(path: str, state: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log.warning("watchdog : état non persisté (%s) — compteur repartira de zéro", exc)


# ── Sondes réelles (httpx) ───────────────────────────────────────────────────

def _probe_url_ok_any_response(url: str, timeout: float):
    """Sonde Caddy LOCAL : Caddy est vivant s'il renvoie une réponse HTTP quelconque
    (même 502 : uvicorn tombé mais Caddy debout). Seule une erreur de connexion/timeout
    signifie « Caddy ne répond pas localement »."""
    import httpx
    try:
        httpx.get(url, timeout=timeout, follow_redirects=False)
        return Probe(True)
    except Exception as exc:  # noqa: BLE001
        return Probe(False, f"{type(exc).__name__}")


def _probe_health(url: str, timeout: float) -> Probe:
    """Sonde publique `/health` par Caddy (chemin réel du client). OK = HTTP 200.
    503 → cause lue dans le JSON (« database: … ») ; autre code / erreur réseau →
    détail court."""
    import httpx
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — injoignable
        return Probe(False, f"injoignable ({type(exc).__name__})")
    if resp.status_code == 200:
        return Probe(True)
    detail = f"HTTP {resp.status_code}"
    try:
        reason = resp.json().get("reason")
        if reason:
            detail = f"{resp.status_code} {reason}"
    except ValueError:
        pass
    return Probe(False, detail)


# ── Actions réelles (systemd + email) ────────────────────────────────────────

def _restart_service(service: str) -> None:
    subprocess.run(["systemctl", "restart", service], check=True, timeout=60)


def _default_notify(reason: str, now: float) -> None:
    """Alerte email réelle (mailer Infomaniak existant), construite PARESSEUSEMENT :
    l'import d'`api` et du mailer n'a lieu QU'au moment d'un vrai redémarrage (rare) —
    un tick sain (chaque minute) ne charge jamais la pile api. Absent d'OPS_EMAIL →
    alerte seulement journalisée (l'auto-guérison n'en dépend pas)."""
    from api import emails
    from api.config import settings
    from api.deps import build_mailer

    if not settings.ops_alert_email:
        log.warning("watchdog : CASAGUIDE_OPS_EMAIL absent → alerte non envoyée "
                    "(journalisée seulement) : %s", reason)
        return
    when = (_dt.datetime.fromtimestamp(now, _dt.timezone.utc)
            .strftime("%d/%m/%Y %H:%M UTC"))
    build_mailer().send(settings.ops_alert_email,
                        emails.watchdog_restart_email(reason, when))
    log.info("watchdog : alerte de redémarrage envoyée à %s",
             settings.ops_alert_email)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watchdog local Holaguia : sonde /health, redémarre le service "
                    "gelé, alerte André (OPS-3).")
    parser.add_argument("--env-file", help="chemin d'un .env à charger (défaut : "
                                           "backend/.env).")
    parser.add_argument("--url", default=HEALTH_URL, help="URL /health à sonder.")
    parser.add_argument("--state", default=STATE_FILE, help="fichier d'état.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)

    state = load_state(args.state)
    new_state = run_once(
        state,
        probe_public=lambda: _probe_health(args.url, PROBE_TIMEOUT_S),
        probe_caddy=lambda: _probe_url_ok_any_response(CADDY_LOCAL_URL, PROBE_TIMEOUT_S),
        restart=_restart_service,
        notify=_default_notify,
        now=time.time())
    save_state(args.state, new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
