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


# ── V2-45 1bis : prompt v2 (doctrine détecteur de bruit) ─────────────────────

def test_prompt_v2_encodes_noise_detector_doctrine():
    prop = {"name": "Villa", "city": "Noordgouwe", "country_code": "NL",
            "lat": 51.71, "lon": 3.91}
    batch = [_poi("1", "Strand", "beach", "approved")]
    batch[0].update(drive_min=45)
    prompt = J.build_prompt(prop, batch, zone_type="RURALE (…)")
    low = prompt.lower()
    # Rôle : détecteur de bruit, pas éditeur ; taux de rejet attendu ~20-25 %.
    assert "détecteur de bruit" in low and "20-25" in prompt
    # Les trois biais du benchmark sont explicitement neutralisés.
    assert "la distance seule" in low          # biais 1 (distance absolue)
    assert "redondance" in low                 # biais 2 (anti-redondance)
    assert "lacune de la source" in low or "source" in low   # biais 3 (métadonnées pauvres)
    # Tolérances par famille présentes.
    assert "~40 min" in prompt and "aucun plafond" in low
    # Le doute profite au maintien.
    assert "doute" in low and "keep" in low


def test_zone_hint_rural_vs_urban_from_harvest_density():
    rural = [_poi("1", "AH", "supermarket", "approved")]
    rural[0].update(drive_min=18)
    assert "RURALE" in J.zone_hint(rural)
    urban = [_poi("1", "AH", "supermarket", "approved")]
    urban[0].update(drive_min=3)
    assert "urbaine" in J.zone_hint(urban).lower()
    # Aucun commerce du quotidien moissonné → indéterminée (jamais d'affirmation gratuite).
    assert "indéterminée" in J.zone_hint(
        [dict(_poi("1", "X", "beach", "approved"), drive_min=5)]).lower()


# ── V2-45 1bis §5 : tout POI reçoit un verdict exploitable ───────────────────

def test_finalize_verdicts_defaults_missing_to_keep_conf_zero():
    pois = [_poi("1", "A", "cafe", "approved"), _poi("2", "B", "cafe", "approved")]
    verdicts = {"1": J.Verdict("reject", 0.9, "bruit")}   # "2" manque
    complete, defaulted = J.finalize_verdicts(pois, verdicts)
    assert defaulted == ["2"]
    assert complete["2"] == J.DEFAULT_VERDICT
    assert complete["2"].verdict == "keep" and complete["2"].confidence == 0.0
    # L'existant n'est pas touché.
    assert complete["1"].verdict == "reject"


def test_metrics_with_defaults_zero_unjudged_and_signalled():
    pois = [_poi("1", "A", "cafe", "approved"), _poi("2", "B", "cafe", "rejected")]
    verdicts = {"1": J.Verdict("keep", 0.9, "ok")}         # "2" manquera
    complete, defaulted = J.finalize_verdicts(pois, verdicts)
    m = J.compute_metrics(pois, complete, defaulted)
    assert m.unjudged == [] and m.judged == 2              # 0 non jugé (recette)
    assert m.defaulted == ["2"]
    # Le défaut 'keep' sur un POI rejeté par André = faux positif (jamais faux rejet).
    assert [r["id"] for r in m.false_keep] == ["2"] and m.false_reject == []
    report = J.render_report({"name": "V", "city": "X", "country_code": "NL"},
                             "PID", m, 1.0, "m", "2026-09-07 10:00")
    assert "verdict par défaut" in report and "non jugé" not in report.split("⚠")[0]


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


# ── V2-45 volet 1ter : quatre retouches du prompt + retry avant défaut ────────

def test_prompt_1ter_heavy_transport_and_g_destination_split():
    prop = {"name": "V", "city": "X", "country_code": "ES", "lat": 38.0, "lon": -0.9}
    prompt = J.build_prompt(prop, [_poi("1", "A", "airport", "approved")],
                            zone_type="urbaine")
    low = " ".join(prompt.lower().split())    # espaces normalisés (le prompt est retourné à la ligne)
    # Retouche 1 : transports lourds sans plafond MAIS 1-2 principaux, surnombre = bruit.
    assert "1 ou 2 principaux" in low and "surnum" in low
    # Retouche 2 : destinations G sans plafond ; proximité de loisir lointaine = bruit.
    assert "destinations de loisir" in low and "aire de jeux à 49 min" in low
    assert "équipement de proximité de loisir" in low
    # Retouche 3 : généricité relative à la catégorie (nom fonctionnel légitime).
    assert "parada de taxis" in low and "relative" in low
    assert "infrastructure fonctionnelle" in low


def test_retry_resubmits_missing_before_defaulting():
    """§4 : un POI non rendu au 1er passage est RE-SOUMIS une fois ; s'il revient, pas
    de verdict par défaut."""
    prop = {"name": "V", "city": "X", "country_code": "NL", "lat": 51.7, "lon": 3.9}
    pois = [_poi("a", "A", "cafe", "approved"), _poi("b", "B", "cafe", "approved")]
    calls = {"n": 0}

    def ask(prompt):
        calls["n"] += 1
        ids = re.findall(r'id "([^"]+)"', prompt)
        # 1er appel : "b" manque (raté de parsing) ; retry : "b" est rendu.
        out = [i for i in ids if not (calls["n"] == 1 and i == "b")]
        return ({"verdicts": [{"id": i, "verdict": "keep", "confidence": 0.8,
                               "reason": "r"} for i in out]},
                {"attempts": [{"units": 10, "cost_cts": 0.1}]})

    verdicts, attempts = J.judge_pois(prop, pois, ask, batch_size=15)
    complete, defaulted = J.finalize_verdicts(pois, verdicts)
    assert calls["n"] == 2                       # un passage + un retry
    assert "b" in verdicts and defaulted == []   # récupéré par le retry, pas défaut
    assert len(attempts) == 2                    # coût des deux passages compté


def test_retry_bounded_then_defaults_if_still_missing():
    """§4 : le retry est BORNÉ à une passe ; un POI toujours muet retombe sur le défaut."""
    prop = {"name": "V", "city": "X", "country_code": "NL", "lat": 51.7, "lon": 3.9}
    pois = [_poi("a", "A", "cafe", "approved"), _poi("b", "B", "cafe", "rejected")]
    calls = {"n": 0}

    def ask(prompt):
        calls["n"] += 1
        ids = re.findall(r'id "([^"]+)"', prompt)
        out = [i for i in ids if i != "b"]       # "b" jamais rendu
        return ({"verdicts": [{"id": i, "verdict": "keep", "confidence": 0.9,
                               "reason": "r"} for i in out]},
                {"attempts": [{"units": 10, "cost_cts": 0.1}]})

    verdicts, _ = J.judge_pois(prop, pois, ask, batch_size=15)
    complete, defaulted = J.finalize_verdicts(pois, verdicts)
    assert calls["n"] == 2                        # une seule passe de retry (pas de boucle)
    assert defaulted == ["b"] and complete["b"] == J.DEFAULT_VERDICT
