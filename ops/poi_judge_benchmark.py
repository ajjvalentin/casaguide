#!/usr/bin/env python3
"""Juge IA du flux POI — BENCHMARK hors pipeline (V2-45 volet 1). LECTURE SEULE.

Le triage humain d'Op de Boerderie (111 examinés → 86 retenus, 25 rejetés) est une
vérité terrain rare : chaque décision a été prise par André sur un cas réel. AVANT
d'insérer un juge IA dans le pipeline, on MESURE sa capacité à retrouver ces décisions.

Ce script soumet chaque POI ARBITRÉ d'un logement (statut approved/edited = retenu,
ou rejected) au jugement de Claude, puis compare le verdict au statut réel. **Le statut
n'est JAMAIS montré au juge** — il ne sert qu'à la comparaison après coup.

STRICTEMENT en lecture seule sur les POI : la seule écriture est la comptabilité dans
`api_costs` (provider 'anthropic', operation 'poi_judge_benchmark') — jamais un UPDATE
ni un DELETE de POI. Rejouable (relancer = re-benchmark, utile pour itérer le prompt).

Métrique CRITIQUE : le taux de FAUX REJETS (le juge rejette ce qu'André a retenu) —
c'est l'erreur qui détruirait de la valeur en production. Les faux positifs (le juge
garde ce qu'André a rejeté) sont moins graves : le pré-tri les relègue, il ne les
détruit pas.

Usage (sur le serveur, dans le venv de l'app) :

    /opt/casaguide/.venv/bin/python /opt/casaguide/ops/poi_judge_benchmark.py \
        --property-id e90bdb88-66d8-4a5e-8234-855fb4d460ee
    …/poi_judge_benchmark.py --property-id <uuid> --dry-run   # plan, aucun appel API

Connexion : DSN dans `CASAGUIDE_DB` (défaut `postgresql:///casaguide`). Charge
`backend/.env` lui-même (hors EnvironmentFile systemd, OPS-1). Le rapport est écrit
en markdown (défaut : à côté, `poi_judge_<propid>_<date>.md`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import dict_row

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                    # ops/ (import opsenv)
sys.path.insert(0, str(_HERE.parent / "backend"))  # backend/ (import enrich.*)
import opsenv  # noqa: E402

log = logging.getLogger("casaguide.poi_judge")

RETAINED = ("approved", "edited")   # André a RETENU (le guide n'affiche que ceux-là)
REJECTED = "rejected"
OPERATION = "poi_judge_benchmark"
_JUDGE_MAX_TOKENS = 4000            # sortie JSON d'un lot de verdicts


def _default_dsn() -> str:
    return os.getenv("CASAGUIDE_DB", "postgresql:///casaguide")


# ── Extraction (SELECT seul — jamais d'écriture de POI) ───────────────────────

def load_property(conn, property_id: str) -> dict | None:
    return conn.execute(
        """SELECT name, city, region, country_code,
                  ST_Y(geom) AS lat, ST_X(geom) AS lon, geocode_accuracy
           FROM properties WHERE id = %s""", (property_id,)).fetchone()


def load_arbitrated_pois(conn, property_id: str) -> list[dict]:
    """POI déjà arbitrés (retenus approved/edited OU rejected), avec de quoi juger.
    Le `status` est chargé POUR LA COMPARAISON — il ne sera jamais mis dans le prompt."""
    return conn.execute(
        """SELECT id::text AS id, name, category_code, address, locality,
                  walk_min, drive_min, source, description_md, status
           FROM pois
           WHERE property_id = %s AND status IN ('approved', 'edited', 'rejected')
           ORDER BY category_code, name""", (property_id,)).fetchall()


# ── Prompt du juge (le STATUT n'y figure JAMAIS) ─────────────────────────────

_CRITERIA = """\
Tu es un relecteur qui décide, pour un guide d'accueil de logement de vacances, si un
lieu mérite d'être proposé au voyageur. Juge CHAQUE lieu selon :
- PERTINENCE pour un vacancier séjournant à CETTE adresse (pas un habitant, pas un pro) ;
- NOM : un lieu sans nom propre (« Aire de jeux », « Parking ») n'a guère de valeur ;
- COHÉRENCE catégorie / réalité (une agence taggée « marché », une base militaire en
  « aéroport »… sont hors sujet) ;
- DISTANCE RAISONNABLE POUR L'USAGE : un commissariat ou une pharmacie au plus proche
  suffit ; un restaurant à 40 min n'aide personne ; une plage ou un site d'excursion
  peut être plus loin. En zone rurale, une certaine distance est normale — ne rejette
  pas un lieu utile juste parce qu'il n'est pas à 2 minutes.
Dans le doute sur un lieu plausiblement utile, tends vers KEEP : mieux vaut le laisser
que détruire de la valeur."""


def build_prompt(prop: dict, batch: list[dict]) -> str:
    """Construit le prompt d'un lot. Contexte du logement + critères + la liste des
    lieux SANS leur statut. Demande un JSON strict `{"verdicts": [...]}`."""
    zone = f'{prop.get("city") or "?"}'
    if prop.get("region"):
        zone += f', {prop["region"]}'
    zone += f' ({prop.get("country_code") or "?"})'
    coords = ""
    if prop.get("lat") is not None and prop.get("lon") is not None:
        coords = f' — coordonnées {prop["lat"]:.4f},{prop["lon"]:.4f}'
    lines = []
    for p in batch:
        dist = []
        if p.get("walk_min") is not None:
            dist.append(f'{p["walk_min"]} min à pied')
        if p.get("drive_min") is not None:
            dist.append(f'{p["drive_min"]} min en voiture')
        dist_txt = f' — {", ".join(dist)}' if dist else " — distance inconnue"
        loc = f' [{p["locality"]}]' if p.get("locality") else ""
        addr = f' — {p["address"]}' if p.get("address") else ""
        desc = (p.get("description_md") or "").strip().replace("\n", " ")
        desc_txt = f' — « {desc[:160]} »' if desc else ""
        lines.append(
            f'- id "{p["id"]}" : {p["name"]}{loc} (catégorie {p["category_code"]}, '
            f'source {p.get("source") or "?"}){addr}{dist_txt}{desc_txt}')
    poi_block = "\n".join(lines)
    return (
        f"{_CRITERIA}\n\n"
        f"LOGEMENT : {prop.get('name') or 'logement'} à {zone}{coords}.\n"
        f"Type : location de vacances. Déduis toi-même le caractère rural ou urbain "
        f"de la zone à partir de la commune et des coordonnées.\n\n"
        f"LIEUX À JUGER ({len(batch)}) :\n{poi_block}\n\n"
        f"Réponds UNIQUEMENT par un objet JSON valide, sans markdown :\n"
        f'{{"verdicts": [{{"id": "...", "verdict": "keep" ou "reject", '
        f'"confidence": 0.0 à 1.0, "reason": "une phrase courte"}}]}}\n'
        f"Un verdict par id fourni, ni plus ni moins.")


# ── Parsing d'un verdict ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Verdict:
    verdict: str          # 'keep' | 'reject'
    confidence: float     # 0..1
    reason: str


def parse_verdicts(data: dict) -> dict[str, Verdict]:
    """Extrait `{id: Verdict}` d'un objet `{"verdicts":[...]}`. Tolère les champs
    manquants (verdict inconnu → 'reject' par défaut le plus PRUDENT pour la mesure ?
    non : on marque 'invalide' et on l'ignore, l'id restera « non jugé »)."""
    out: dict[str, Verdict] = {}
    for v in (data or {}).get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or "").strip()
        verdict = str(v.get("verdict") or "").strip().lower()
        if not vid or verdict not in ("keep", "reject"):
            continue
        try:
            conf = float(v.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        out[vid] = Verdict(verdict, conf, str(v.get("reason") or "").strip())
    return out


# ── Jugement (l'appel Claude est INJECTÉ → testable sans réseau) ──────────────

def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def judge_pois(prop: dict, pois: list[dict],
               ask: Callable[[str], tuple[dict, dict]], *,
               batch_size: int = 15) -> tuple[dict[str, Verdict], list[dict]]:
    """Juge les POI par lots. `ask(prompt) -> (data, meta)` est injecté (réel : appel
    Claude ; test : bouchon). Renvoie ({id: Verdict}, attempts) où `attempts` est la
    liste des coûts par essai à comptabiliser dans `api_costs`."""
    verdicts: dict[str, Verdict] = {}
    attempts: list[dict] = []
    batches = list(_chunks(pois, batch_size))
    for n, batch in enumerate(batches, 1):
        log.info("· lot %d/%d (%d lieux)…", n, len(batches), len(batch))
        data, meta = ask(build_prompt(prop, batch))
        verdicts.update(parse_verdicts(data))
        attempts.extend(meta.get("attempts")
                        or [{"units": meta.get("units", 0),
                             "cost_cts": meta.get("cost_cts", 0.0)}])
    return verdicts, attempts


# ── Métriques (PUR) ───────────────────────────────────────────────────────────

@dataclass
class Metrics:
    total: int
    judged: int
    unjudged: list[str]
    human_keeps: int
    human_rejects: int
    agree: int
    agree_keep: int
    agree_reject: int
    false_reject: list[dict] = field(default_factory=list)   # humain KEEP, juge REJECT
    false_keep: list[dict] = field(default_factory=list)     # humain REJECT, juge KEEP
    by_confidence: list[dict] = field(default_factory=list)

    @property
    def agreement_pct(self) -> float:
        return round(100.0 * self.agree / self.judged, 1) if self.judged else 0.0

    @property
    def false_reject_rate(self) -> float:
        """Taux de faux rejets sur ce qu'André a RETENU (la métrique critique)."""
        return round(100.0 * len(self.false_reject) / self.human_keeps, 1) \
            if self.human_keeps else 0.0

    @property
    def disagreements(self) -> list[dict]:
        return self.false_reject + self.false_keep


_CONF_BUCKETS = ((0.9, 1.01, "≥ 0,90"), (0.7, 0.9, "0,70–0,90"),
                 (0.5, 0.7, "0,50–0,70"), (0.0, 0.5, "< 0,50"))


def compute_metrics(pois: list[dict], verdicts: dict[str, Verdict]) -> Metrics:
    """Compare verdicts et statuts réels. PUR (aucune E/S)."""
    human_keeps = sum(1 for p in pois if p["status"] in RETAINED)
    human_rejects = sum(1 for p in pois if p["status"] == REJECTED)
    unjudged = [p["id"] for p in pois if p["id"] not in verdicts]
    m = Metrics(total=len(pois), judged=len(pois) - len(unjudged), unjudged=unjudged,
                human_keeps=human_keeps, human_rejects=human_rejects,
                agree=0, agree_keep=0, agree_reject=0)
    buckets = {label: [0, 0] for _, _, label in _CONF_BUCKETS}  # label -> [agree, n]
    for p in pois:
        v = verdicts.get(p["id"])
        if v is None:
            continue
        human_keep = p["status"] in RETAINED
        judge_keep = v.verdict == "keep"
        row = {"id": p["id"], "name": p["name"], "category": p["category_code"],
               "status": p["status"], "confidence": v.confidence, "reason": v.reason}
        if human_keep and judge_keep:
            m.agree += 1; m.agree_keep += 1
        elif not human_keep and not judge_keep:
            m.agree += 1; m.agree_reject += 1
        elif human_keep and not judge_keep:
            m.false_reject.append(row)          # CRITIQUE
        else:
            m.false_keep.append(row)
        for lo, hi, label in _CONF_BUCKETS:
            if lo <= v.confidence < hi:
                buckets[label][1] += 1
                if human_keep == judge_keep:
                    buckets[label][0] += 1
                break
    m.by_confidence = [
        {"bucket": label, "n": n, "agree": a,
         "pct": round(100.0 * a / n, 1) if n else 0.0}
        for _, _, label in _CONF_BUCKETS
        for a, n in [buckets[label]]]
    return m


# ── Rapport markdown (PUR) ────────────────────────────────────────────────────

def render_report(prop: dict, property_id: str, metrics: Metrics,
                  cost_cts: float, model: str, when: str) -> str:
    m = metrics
    L: list[str] = []
    L.append(f"# Benchmark juge IA — {prop.get('name') or property_id}")
    L.append("")
    L.append(f"- Logement : `{property_id}` — {prop.get('city') or '?'} "
             f"({prop.get('country_code') or '?'})")
    L.append(f"- Modèle juge : `{model}` · exécuté le {when}")
    L.append(f"- POI arbitrés : **{m.total}** ({m.human_keeps} retenus, "
             f"{m.human_rejects} rejetés)"
             + (f" · {len(m.unjudged)} non jugé(s)" if m.unjudged else ""))
    L.append("")
    # 1. Accord global
    L.append("## 1. Accord global")
    L.append(f"**{m.agreement_pct} %** des verdicts (sur {m.judged} jugés) coïncident "
             f"avec le triage humain.")
    L.append("")
    # 2. Matrice de confusion + faux rejets (métrique critique)
    L.append("## 2. Matrice de confusion")
    L.append("")
    L.append("| | Juge : KEEP | Juge : REJECT |")
    L.append("|---|---|---|")
    L.append(f"| **André : RETENU** | {m.agree_keep} ✔ | "
             f"**{len(m.false_reject)} FAUX REJET** ✗ |")
    L.append(f"| **André : REJETÉ** | {len(m.false_keep)} (faux positif) | "
             f"{m.agree_reject} ✔ |")
    L.append("")
    L.append(f"- **Taux de faux rejets : {m.false_reject_rate} %** "
             f"({len(m.false_reject)}/{m.human_keeps} retenus) — *métrique critique* : "
             f"c'est la valeur détruite en production.")
    L.append(f"- Faux positifs : {len(m.false_keep)}/{m.human_rejects} rejetés "
             f"(moins graves : le pré-tri les relègue, il ne les détruit pas).")
    L.append("")
    # 3. Désaccords nominatifs
    L.append("## 3. Désaccords (lecture humaine)")
    if not m.disagreements:
        L.append("*Aucun désaccord.*")
    else:
        L.append("### Faux rejets (André a retenu, le juge rejette) — À SCRUTER")
        L.append(_disagreement_table(m.false_reject) if m.false_reject else "*Aucun.*")
        L.append("")
        L.append("### Faux positifs (André a rejeté, le juge garde)")
        L.append(_disagreement_table(m.false_keep) if m.false_keep else "*Aucun.*")
    L.append("")
    # 4. Coût
    L.append("## 4. Coût de la passe")
    L.append(f"**{cost_cts:.2f} ct** ({cost_cts / 100:.4f} €) — comptabilisé dans "
             f"`api_costs` (operation `{OPERATION}`). C'est la donnée qui conditionne "
             f"l'économie du futur guide locataires.")
    L.append("")
    # 5. Accord par tranche de confiance
    L.append("## 5. Accord par tranche de confiance")
    L.append("")
    L.append("| Confiance | Jugés | Accord |")
    L.append("|---|---|---|")
    for b in m.by_confidence:
        L.append(f"| {b['bucket']} | {b['n']} | "
                 f"{b['pct']} % ({b['agree']}/{b['n']}) |")
    if m.unjudged:
        L.append("")
        L.append(f"> {len(m.unjudged)} POI sans verdict exploitable (ignorés du calcul "
                 f"d'accord).")
    L.append("")
    return "\n".join(L)


def _disagreement_table(rows: list[dict]) -> str:
    out = ["| Lieu | Catégorie | Statut réel | Conf. | Motif du juge |",
           "|---|---|---|---|---|"]
    for r in rows:
        reason = (r["reason"] or "").replace("|", "/")
        out.append(f"| {r['name']} | {r['category']} | {r['status']} | "
                   f"{r['confidence']:.2f} | {reason} |")
    return "\n".join(out)


# ── Orchestration (une seule écriture : api_costs) ────────────────────────────

def run_benchmark(conn, property_id: str, ask: Callable[[str], tuple[dict, dict]], *,
                  model: str, batch_size: int = 15,
                  when: str | None = None) -> tuple[str, float, Metrics]:
    """Lit les POI arbitrés, les fait juger (ask injecté), calcule les métriques,
    comptabilise le coût dans `api_costs` (SEULE écriture) et rend le rapport markdown.
    Renvoie (rapport, coût_cts, métriques). Aucun POI n'est modifié."""
    prop = load_property(conn, property_id)
    if prop is None:
        raise LookupError(f"Logement introuvable : {property_id}")
    pois = load_arbitrated_pois(conn, property_id)
    if not pois:
        raise LookupError(f"Aucun POI arbitré (approved/edited/rejected) pour {property_id}")

    verdicts, attempts = judge_pois(prop, pois, ask, batch_size=batch_size)
    # SEULE écriture : la comptabilité (une ligne par essai). job_id NULL (hors job).
    import enrich.db as edb  # noqa: PLC0415 — import tardif (après chargement .env)
    edb.record_costs(conn, property_id, None, "anthropic", OPERATION, attempts)
    conn.commit()

    cost_cts = round(sum(a.get("cost_cts", 0.0) for a in attempts), 4)
    metrics = compute_metrics(pois, verdicts)
    when = when or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    report = render_report(prop, property_id, metrics, cost_cts, model, when)
    return report, cost_cts, metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark juge IA du flux POI contre le triage humain (V2-45 v1, "
                    "lecture seule hors api_costs).")
    parser.add_argument("--property-id", required=True)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument("--out", default=None,
                        help="fichier du rapport (défaut : poi_judge_<id>_<date>.md).")
    parser.add_argument("--dry-run", action="store_true",
                        help="liste les POI et les lots, AUCUN appel API ni rapport.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)
    dsn = args.dsn or _default_dsn()

    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        prop = load_property(conn, args.property_id)
        if prop is None:
            log.error("✗ logement introuvable : %s", args.property_id)
            return 2
        pois = load_arbitrated_pois(conn, args.property_id)
        n_keep = sum(1 for p in pois if p["status"] in RETAINED)
        log.info("· %d POI arbitrés (%d retenus, %d rejetés), lots de %d.",
                 len(pois), n_keep, len(pois) - n_keep, args.batch_size)
        if args.dry_run:
            n_batches = (len(pois) + args.batch_size - 1) // max(1, args.batch_size)
            log.info("· DRY-RUN : %d lot(s) seraient jugés — aucun appel API.", n_batches)
            return 0
        if not pois:
            log.error("✗ aucun POI arbitré à juger.")
            return 3

        # Appel Claude réel (chemin JSON robuste de l'enrichissement).
        import anthropic  # noqa: PLC0415
        from enrich import claude_enrich  # noqa: PLC0415
        from enrich.settings import settings  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = settings.anthropic_model

        def ask(prompt: str) -> tuple[dict, dict]:
            return claude_enrich._ask_json(client, prompt, max_tokens=_JUDGE_MAX_TOKENS)

        try:
            report, cost_cts, metrics = run_benchmark(
                conn, args.property_id, ask, model=model, batch_size=args.batch_size)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    out = Path(args.out) if args.out else (
        _HERE / f"poi_judge_{args.property_id}_{_dt.date.today().isoformat()}.md")
    out.write_text(report, encoding="utf-8")
    log.info("✔ accord %.1f %% · faux rejets %.1f %% · coût %.2f ct → %s",
             metrics.agreement_pct, metrics.false_reject_rate, cost_cts, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
