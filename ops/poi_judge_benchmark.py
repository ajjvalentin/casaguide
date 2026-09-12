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
# Cœur PARTAGÉ du juge (V2-54) : prompt, parsing, jugement par lots. Extrait dans
# enrich/ pour que le pipeline (offre Guide Voyageur) et ce benchmark jugent à
# l'IDENTIQUE — une seule vérité du prompt. Les métriques/rapport restent ici.
from enrich.judge import (  # noqa: E402
    DEFAULT_VERDICT, Verdict, build_prompt, finalize_verdicts, judge_pois,
    parse_verdicts, zone_hint, _CRITERIA, _EVERYDAY_CATEGORIES, _JUDGE_MAX_TOKENS)

log = logging.getLogger("casaguide.poi_judge")

RETAINED = ("approved", "edited")   # André a RETENU (le guide n'affiche que ceux-là)
REJECTED = "rejected"
OPERATION = "poi_judge_benchmark"
OPERATION_DUMP = "poi_verdict_dump"  # V2-49 : dump des verdicts (industrialisation du tri)
# _JUDGE_MAX_TOKENS, le prompt (_CRITERIA), le parsing et judge_pois vivent désormais
# dans enrich.judge (importés en tête) — cœur partagé avec le pipeline (V2-54).
# Estimation de coût (V2-49) : mesure V2-45 ≈ 15 ct pour ~90 POI ≈ 0,17 ct/POI ; on arrondit
# PRUDEMMENT à la hausse pour le devis avant une passe parc (le coût réel est comptabilisé).
_EST_CT_PER_POI = 0.20


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


def load_all_pois(conn, property_id: str) -> list[dict]:
    """TOUS les POI d'un logement, quel que soit le statut — `suggested` INCLUS (V2-49 :
    le dump propose des verdicts sur le flux entier, pas seulement l'arbitré). Le `status`
    est chargé pour le RAPPORT (contexte de l'humain) mais reste invisible du juge."""
    return conn.execute(
        """SELECT id::text AS id, name, category_code, address, locality,
                  walk_min, drive_min, source, description_md, status
           FROM pois WHERE property_id = %s
           ORDER BY category_code, name""", (property_id,)).fetchall()


def list_properties_with_pois(conn) -> list[dict]:
    """Logements ayant au moins un POI (V2-49 mode parc), avec leur volume."""
    return conn.execute(
        """SELECT p.id::text AS id, p.name, count(po.id) AS n_pois
           FROM properties p JOIN pois po ON po.property_id = p.id
           GROUP BY p.id, p.name ORDER BY p.name""").fetchall()


def estimate_parc_cost(properties: list[dict], batch_size: int) -> dict:
    """Devis AVANT lancement d'une passe parc (V2-49) : nb de logements, de POI, d'appels
    LLM (lots) et coût ESTIMÉ (le coût réel est comptabilisé dans api_costs à l'exécution)."""
    n_pois = sum(p["n_pois"] for p in properties)
    n_calls = sum((p["n_pois"] + batch_size - 1) // batch_size
                  for p in properties if p["n_pois"])
    return {"n_props": len(properties), "n_pois": n_pois, "n_calls": n_calls,
            "est_ct": round(n_pois * _EST_CT_PER_POI, 1)}


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
    defaulted: list[str] = field(default_factory=list)       # verdict par défaut (§5)

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


def compute_metrics(pois: list[dict], verdicts: dict[str, Verdict],
                    defaulted: list[str] | None = None) -> Metrics:
    """Compare verdicts et statuts réels. PUR (aucune E/S). `defaulted` = ids ayant reçu
    le verdict par défaut (§5) — comptés dans l'accord (ils ont un verdict `keep`) mais
    signalés à part. `unjudged` reste pour un id RÉELLEMENT sans verdict (défensif :
    vide après `finalize_verdicts`)."""
    human_keeps = sum(1 for p in pois if p["status"] in RETAINED)
    human_rejects = sum(1 for p in pois if p["status"] == REJECTED)
    unjudged = [p["id"] for p in pois if p["id"] not in verdicts]
    m = Metrics(total=len(pois), judged=len(pois) - len(unjudged), unjudged=unjudged,
                human_keeps=human_keeps, human_rejects=human_rejects,
                agree=0, agree_keep=0, agree_reject=0,
                defaulted=list(defaulted or []))
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
             + (f" · {len(m.defaulted)} verdict(s) par défaut" if m.defaulted else "")
             + (f" · ⚠ {len(m.unjudged)} non jugé(s)" if m.unjudged else ""))
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
    if m.defaulted:
        L.append("")
        L.append(f"> {len(m.defaulted)} POI ont reçu le **verdict par défaut** "
                 f"(`keep` confiance 0 — non rendus par le juge, §5) : comptés dans "
                 f"l'accord mais à re-soumettre.")
    if m.unjudged:
        L.append("")
        L.append(f"> ⚠ {len(m.unjudged)} POI SANS aucun verdict (anomalie).")
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
    # §5 : tout POI reçoit un verdict exploitable (défaut `keep` conf 0, signalé).
    verdicts, defaulted = finalize_verdicts(pois, verdicts)
    # SEULE écriture : la comptabilité (une ligne par essai). job_id NULL (hors job).
    import enrich.db as edb  # noqa: PLC0415 — import tardif (après chargement .env)
    edb.record_costs(conn, property_id, None, "anthropic", OPERATION, attempts)
    conn.commit()

    cost_cts = round(sum(a.get("cost_cts", 0.0) for a in attempts), 4)
    metrics = compute_metrics(pois, verdicts, defaulted)
    when = when or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    report = render_report(prop, property_id, metrics, cost_cts, model, when)
    return report, cost_cts, metrics


# ── Dump des verdicts par logement (V2-49 : le système PROPOSE, l'humain DISPOSE) ─

def render_verdict_dump(prop: dict, property_id: str, pois: list[dict],
                        verdicts: dict, cost_cts: float, model: str,
                        when: str) -> tuple[str, int]:
    """Markdown de travail : les RETRAITS proposés EN TÊTE (liste de travail de l'humain),
    puis les maintiens par catégorie. Le `status` actuel est affiché comme CONTEXTE (jamais
    montré au juge). Renvoie (markdown, nb de retraits). PUR."""
    from itertools import groupby
    removals = [p for p in pois if verdicts[p["id"]].verdict == "reject"]
    keeps = [p for p in pois if verdicts[p["id"]].verdict == "keep"]
    removals.sort(key=lambda p: (p["category_code"], -verdicts[p["id"]].confidence))
    keeps.sort(key=lambda p: (p["category_code"], p["name"] or ""))

    L: list[str] = []
    L.append(f"# Verdicts proposés — {prop.get('name') or property_id}")
    L.append("")
    L.append(f"- `{property_id}` — {prop.get('city') or '?'} "
             f"({prop.get('country_code') or '?'}) · modèle `{model}` · {when}")
    L.append(f"- {len(pois)} POI jugés · **{len(removals)} retrait(s) proposé(s)** · "
             f"coût {cost_cts:.2f} ct")
    L.append("- *Le système PROPOSE, l'humain DISPOSE : ci-dessous la liste de travail "
             "(retraits proposés), puis les maintiens par catégorie.*")
    L.append("")
    L.append(f"## Retraits proposés ({len(removals)}) — liste de travail")
    if not removals:
        L.append("*Aucun retrait proposé.*")
    else:
        L.append("| Lieu | Catégorie | Conf. | Statut actuel | Motif du juge |")
        L.append("|---|---|--:|---|---|")
        for p in removals:
            v = verdicts[p["id"]]
            reason = (v.reason or "").replace("|", "/")
            L.append(f"| {p['name']} | {p['category_code']} | {v.confidence:.2f} | "
                     f"{p.get('status') or '?'} | {reason} |")
    L.append("")
    L.append(f"## Maintiens par catégorie ({len(keeps)})")
    L.append("")
    for cat, grp in groupby(keeps, key=lambda p: p["category_code"]):
        members = list(grp)
        L.append(f"### {cat} ({len(members)})")
        for p in members:
            v = verdicts[p["id"]]
            tail = f" · {v.reason}" if v.reason else ""
            L.append(f"- {p['name']} — keep ({v.confidence:.2f}){tail}")
        L.append("")
    return "\n".join(L), len(removals)


def run_dump(conn, property_id: str, ask: Callable[[str], tuple[dict, dict]], *,
             model: str, batch_size: int = 15,
             when: str | None = None) -> tuple[str, float, int, int]:
    """Juge TOUS les POI d'un logement (suggested inclus) et rend le dump de verdicts.
    Renvoie (markdown, coût_cts, nb_pois, nb_retraits). Lecture seule hors api_costs
    (operation 'poi_verdict_dump'). Aucun POI modifié."""
    prop = load_property(conn, property_id)
    if prop is None:
        raise LookupError(f"Logement introuvable : {property_id}")
    pois = load_all_pois(conn, property_id)
    if not pois:
        raise LookupError(f"Aucun POI pour {property_id}")
    verdicts, attempts = judge_pois(prop, pois, ask, batch_size=batch_size)
    verdicts, _defaulted = finalize_verdicts(pois, verdicts)
    import enrich.db as edb  # noqa: PLC0415
    edb.record_costs(conn, property_id, None, "anthropic", OPERATION_DUMP, attempts)
    conn.commit()
    cost_cts = round(sum(a.get("cost_cts", 0.0) for a in attempts), 4)
    when = when or _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    report, n_removals = render_verdict_dump(prop, property_id, pois, verdicts,
                                             cost_cts, model, when)
    return report, cost_cts, len(pois), n_removals


def _build_ask():
    """Client Claude réel + fonction `ask` (chemin JSON robuste de l'enrichissement).
    Renvoie (ask, client, model). Le client est à fermer par l'appelant."""
    import anthropic  # noqa: PLC0415
    from enrich import claude_enrich  # noqa: PLC0415
    from enrich.settings import settings  # noqa: PLC0415
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def ask(prompt: str) -> tuple[dict, dict]:
        return claude_enrich._ask_json(client, prompt, max_tokens=_JUDGE_MAX_TOKENS)
    return ask, client, settings.anthropic_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Juge IA du flux POI : benchmark (défaut) OU dump des verdicts "
                    "(--dump-verdicts / --all-properties). Lecture seule hors api_costs.")
    parser.add_argument("--property-id", default=None)
    parser.add_argument("--all-properties", action="store_true",
                        help="V2-49 : dump des verdicts sur TOUT le parc (un fichier par "
                             "logement). Implique --dump-verdicts.")
    parser.add_argument("--dump-verdicts", action="store_true",
                        help="V2-49 : dump des verdicts (TOUS les POI, suggested inclus ; "
                             "retraits proposés en tête) au lieu du benchmark.")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument("--out", default=None,
                        help="fichier (mode 1 logement) ou RÉPERTOIRE (mode parc).")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan + coût estimé, AUCUN appel API ni fichier.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opsenv.load_env(args.env_file)
    dsn = args.dsn or _default_dsn()
    if not args.property_id and not args.all_properties:
        log.error("✗ précisez --property-id <uuid> ou --all-properties.")
        return 2
    dump_mode = args.dump_verdicts or args.all_properties
    stamp = _dt.date.today().isoformat()

    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        log.error("✗ connexion à la base impossible : %s", exc)
        return 1

    with conn:
        # ── Mode PARC (V2-49) : dump un fichier par logement ────────────────
        if args.all_properties:
            props = list_properties_with_pois(conn)
            est = estimate_parc_cost(props, args.batch_size)
            log.info("· parc : %d logement(s) avec POI, %d POI, ~%d appel(s) LLM ; "
                     "coût ESTIMÉ ~%.1f ct.", est["n_props"], est["n_pois"],
                     est["n_calls"], est["est_ct"])
            if args.dry_run:
                log.info("· DRY-RUN : aucun appel API, aucun fichier.")
                return 0
            out_dir = Path(args.out) if args.out else _HERE
            out_dir.mkdir(parents=True, exist_ok=True)
            ask, client, model = _build_ask()
            total_ct = 0.0
            try:
                for p in props:
                    report, ct, n_pois, n_rem = run_dump(
                        conn, p["id"], ask, model=model, batch_size=args.batch_size)
                    (out_dir / f"poi_verdicts_{p['id']}_{stamp}.md").write_text(
                        report, encoding="utf-8")
                    total_ct += ct
                    log.info("  · %s : %d POI, %d retrait(s) proposé(s), %.2f ct",
                             p["name"], n_pois, n_rem, ct)
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("✔ parc dumpé (%d logement(s)) → %s · coût réel %.2f ct",
                     len(props), out_dir, total_ct)
            return 0

        # ── Mode 1 LOGEMENT ─────────────────────────────────────────────────
        prop = load_property(conn, args.property_id)
        if prop is None:
            log.error("✗ logement introuvable : %s", args.property_id)
            return 2
        pois = (load_all_pois if dump_mode else load_arbitrated_pois)(
            conn, args.property_id)
        log.info("· %d POI (%s), lots de %d.", len(pois),
                 "tous statuts" if dump_mode else "arbitrés", args.batch_size)
        if args.dry_run:
            n_batches = (len(pois) + args.batch_size - 1) // max(1, args.batch_size)
            est = round(len(pois) * _EST_CT_PER_POI, 1)
            log.info("· DRY-RUN : %d lot(s), coût estimé ~%.1f ct — aucun appel API.",
                     n_batches, est)
            return 0
        if not pois:
            log.error("✗ aucun POI à juger.")
            return 3

        ask, client, model = _build_ask()
        try:
            if dump_mode:
                report, cost_cts, n_pois, n_rem = run_dump(
                    conn, args.property_id, ask, model=model, batch_size=args.batch_size)
                out = Path(args.out) if args.out else (
                    _HERE / f"poi_verdicts_{args.property_id}_{stamp}.md")
                out.write_text(report, encoding="utf-8")
                log.info("✔ %d POI, %d retrait(s) proposé(s), coût %.2f ct → %s",
                         n_pois, n_rem, cost_cts, out)
            else:
                report, cost_cts, metrics = run_benchmark(
                    conn, args.property_id, ask, model=model, batch_size=args.batch_size)
                out = Path(args.out) if args.out else (
                    _HERE / f"poi_judge_{args.property_id}_{stamp}.md")
                out.write_text(report, encoding="utf-8")
                log.info("✔ accord %.1f %% · faux rejets %.1f %% · coût %.2f ct → %s",
                         metrics.agreement_pct, metrics.false_reject_rate, cost_cts, out)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
