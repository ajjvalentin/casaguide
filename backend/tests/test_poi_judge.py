"""Juge IA du flux POI — benchmark hors pipeline (V2-45 volet 1).

Le cœur (métriques, parsing, rapport, batching) est PUR / injectable → testable sans
réseau. Un test d'intégration prouve la LECTURE SEULE (aucun POI modifié) et l'unique
écriture autorisée (`api_costs`).
"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/
import poi_judge_benchmark as J  # noqa: E402
from enrich.settings import settings  # noqa: E402


# ── Métriques (pures) ─────────────────────────────────────────────────────────

def _poi(pid, name, cat, status):
    return {"id": pid, "name": name, "category_code": cat, "status": status}


def test_compute_metrics_confusion_and_false_reject_rate():
    pois = [
        _poi("1", "Resto", "restaurant", "approved"),   # retenu
        _poi("2", "Poste militaire", "police", "rejected"),  # rejeté
        _poi("3", "Plage", "beach", "edited"),          # retenu
        _poi("4", "Bar", "bar", "approved"),            # retenu, non jugé
    ]
    verdicts = {
        "1": J.Verdict("keep", 0.95, "utile"),
        "2": J.Verdict("reject", 0.80, "hors sujet"),
        "3": J.Verdict("reject", 0.60, "trop loin"),   # FAUX REJET (humain a retenu)
        # "4" absent → non jugé
    }
    m = J.compute_metrics(pois, verdicts)
    assert m.total == 4 and m.judged == 3 and m.unjudged == ["4"]
    assert m.human_keeps == 3 and m.human_rejects == 1
    assert m.agree_keep == 1 and m.agree_reject == 1 and m.agree == 2
    assert [r["id"] for r in m.false_reject] == ["3"]      # le faux rejet, nominatif
    assert m.false_keep == []
    assert m.agreement_pct == round(100 * 2 / 3, 1)        # 66.7
    assert m.false_reject_rate == round(100 * 1 / 3, 1)    # 33.3 (1 sur 3 retenus)
    # Le motif du juge est conservé pour lecture humaine.
    assert m.false_reject[0]["reason"] == "trop loin"


def test_compute_metrics_by_confidence_buckets():
    pois = [_poi("1", "A", "cafe", "approved"), _poi("2", "B", "cafe", "approved"),
            _poi("3", "C", "cafe", "rejected")]
    verdicts = {"1": J.Verdict("keep", 0.95, ""),      # ≥0,90, accord
                "2": J.Verdict("reject", 0.80, ""),    # 0,70–0,90, désaccord
                "3": J.Verdict("reject", 0.40, "")}    # <0,50, accord
    m = J.compute_metrics(pois, verdicts)
    by = {b["bucket"]: b for b in m.by_confidence}
    assert by["≥ 0,90"] == {"bucket": "≥ 0,90", "n": 1, "agree": 1, "pct": 100.0}
    assert by["0,70–0,90"]["n"] == 1 and by["0,70–0,90"]["agree"] == 0
    assert by["< 0,50"]["agree"] == 1


# ── Parsing des verdicts ──────────────────────────────────────────────────────

def test_parse_verdicts_tolerant_and_clamped():
    data = {"verdicts": [
        {"id": "a", "verdict": "KEEP", "confidence": 1.5, "reason": "ok"},   # clampé à 1
        {"id": "b", "verdict": "reject", "confidence": "0.3", "reason": "x"},  # str → float
        {"id": "c", "verdict": "maybe", "confidence": 0.5},   # verdict invalide → ignoré
        {"id": "", "verdict": "keep"},                        # id vide → ignoré
        "pas un dict",                                        # ignoré
    ]}
    out = J.parse_verdicts(data)
    assert set(out) == {"a", "b"}
    assert out["a"].verdict == "keep" and out["a"].confidence == 1.0
    assert out["b"].verdict == "reject" and out["b"].confidence == 0.3
    assert J.parse_verdicts({}) == {}


# ── Batching + cumul du coût (ask injecté) ───────────────────────────────────

def test_judge_pois_batches_and_accumulates_cost():
    prop = {"name": "V", "city": "X", "country_code": "NL", "lat": 51.7, "lon": 3.9}
    pois = [_poi(str(i), f"P{i}", "restaurant", "approved") for i in range(5)]
    calls = []

    def ask(prompt):
        ids = re.findall(r'id "([^"]+)"', prompt)
        calls.append(ids)
        return ({"verdicts": [{"id": i, "verdict": "keep", "confidence": 0.9,
                               "reason": "r"} for i in ids]},
                {"attempts": [{"units": 10, "cost_cts": 0.1}]})

    verdicts, attempts = J.judge_pois(prop, pois, ask, batch_size=2)
    assert len(calls) == 3                     # 5 POI / lots de 2 → 3 lots
    assert [len(c) for c in calls] == [2, 2, 1]
    assert len(verdicts) == 5 and len(attempts) == 3   # un coût par lot
    assert round(sum(a["cost_cts"] for a in attempts), 2) == 0.30


# ── Le prompt ne montre JAMAIS le statut ─────────────────────────────────────

def test_build_prompt_never_leaks_status():
    prop = {"name": "Villa", "city": "Noordgouwe", "region": "Zélande",
            "country_code": "NL", "lat": 51.71, "lon": 3.91}
    batch = [_poi("id-1", "Politie", "police", "rejected"),
             _poi("id-2", "Albert Heijn", "supermarket", "approved")]
    for p in batch:
        p.update(walk_min=10, drive_min=5, address="Rue X", source="osm",
                 description_md="desc")
    prompt = J.build_prompt(prop, batch)
    # Les VALEURS de statut réelles ne fuitent jamais (le juge doit être aveugle).
    for leak in ("approved", "edited", "rejected", "status"):
        assert leak not in prompt, leak
    # Mais les données de jugement sont bien là.
    assert "Politie" in prompt and "Albert Heijn" in prompt and "police" in prompt
    assert "Noordgouwe" in prompt


# ── Rapport : les cinq éléments exigés ───────────────────────────────────────

def test_render_report_has_the_five_sections():
    prop = {"name": "Op de Boerderie", "city": "Noordgouwe", "country_code": "NL"}
    pois = [_poi("1", "A", "restaurant", "approved"), _poi("2", "B", "police", "rejected"),
            _poi("3", "C", "beach", "approved")]
    verdicts = {"1": J.Verdict("keep", 0.9, "ok"), "2": J.Verdict("reject", 0.8, "ok"),
                "3": J.Verdict("reject", 0.5, "trop loin")}
    m = J.compute_metrics(pois, verdicts)
    report = J.render_report(prop, "PID", m, cost_cts=12.34, model="claude-sonnet-4-6",
                             when="2026-09-06 10:00")
    for section in ("## 1. Accord global", "## 2. Matrice de confusion",
                    "## 3. Désaccords", "## 4. Coût de la passe",
                    "## 5. Accord par tranche de confiance"):
        assert section in report, section
    assert "FAUX REJET" in report and "Taux de faux rejets" in report
    assert "12.34 ct" in report and J.OPERATION in report
    assert "trop loin" in report                # le désaccord nominatif figure


# ── Intégration : LECTURE SEULE + api_costs (contre le vrai PostgreSQL) ───────

def test_run_benchmark_is_read_only_except_api_costs():
    oid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    specimens = [("Albert Heijn", "supermarket", "approved"),
                 ("Politie Lointaine", "police", "rejected"),
                 ("Strand Renesse", "beach", "edited"),
                 ("Speeltuin", "family_activity", "rejected")]
    ids: dict[str, str] = {}
    with psycopg.connect(settings.db_dsn, row_factory=dict_row) as conn:
        conn.execute("INSERT INTO owners (id, email, full_name) VALUES (%s,%s,'T')",
                     (oid, f"{oid}@test.local"))
        conn.execute(
            """INSERT INTO properties (id, owner_id, name, address_line1, city,
                   country_code, geom)
               VALUES (%s,%s,'Op de Boerderie','Hanenweg 9','Noordgouwe','NL',
                   ST_SetSRID(ST_MakePoint(3.91,51.71),4326))""", (pid, oid))
        for name, cat, status in specimens:
            r = conn.execute(
                """INSERT INTO pois (property_id, category_code, name, geom, source,
                       status) VALUES (%s,%s,%s,
                       ST_SetSRID(ST_MakePoint(3.91,51.71),4326),'osm',%s)
                   RETURNING id::text AS id""", (pid, cat, name, status)).fetchone()
            ids[name] = r["id"]
        conn.commit()

        # Empreinte AVANT (comptage + statuts).
        before = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id::text AS id, status FROM pois WHERE property_id=%s", (pid,))}

        # Juge bouchonné : garde tout SAUF le nom générique (parse les ids du prompt).
        def ask(prompt):
            pids = re.findall(r'id "([^"]+)"', prompt)
            verdicts = []
            for i in pids:
                keep = i != ids["Speeltuin"]   # rejette la seule aire de jeux
                verdicts.append({"id": i, "verdict": "keep" if keep else "reject",
                                 "confidence": 0.85, "reason": "test"})
            return ({"verdicts": verdicts},
                    {"attempts": [{"units": 20, "cost_cts": 0.5}]})

        try:
            report, cost_cts, metrics = J.run_benchmark(
                conn, pid, ask, model="claude-sonnet-4-6", batch_size=2,
                when="2026-09-06 10:00")

            # LECTURE SEULE : aucun POI modifié (comptage + statuts identiques).
            after = {r["id"]: r["status"] for r in conn.execute(
                "SELECT id::text AS id, status FROM pois WHERE property_id=%s", (pid,))}
            assert after == before

            # SEULE écriture : api_costs (une ligne par lot ; 4 POI / lots de 2 → 2).
            n_costs = conn.execute(
                "SELECT count(*) c, COALESCE(SUM(cost_cts),0) s FROM api_costs "
                "WHERE property_id=%s AND operation=%s", (pid, J.OPERATION)).fetchone()
            assert n_costs["c"] == 2 and float(n_costs["s"]) == 1.0
            assert round(cost_cts, 2) == 1.0

            # Le juge garde tout sauf Speeltuin (rejeté par les deux → accord). Politie,
            # rejeté par André mais gardé par le juge, est un FAUX POSITIF (désaccord non
            # grave). Aucun FAUX REJET (rien de retenu n'a été rejeté) — le point critique.
            assert metrics.total == 4 and metrics.judged == 4
            assert metrics.false_reject == []            # métrique critique : 0
            assert [r["name"] for r in metrics.false_keep] == ["Politie Lointaine"]
            assert metrics.agreement_pct == 75.0         # 3/4 (AH, Strand, Speeltuin)
            assert metrics.false_reject_rate == 0.0
            assert "## 1. Accord global" in report
        finally:
            conn.execute("DELETE FROM owners WHERE id=%s", (oid,))
            conn.commit()
