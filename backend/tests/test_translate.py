"""Tests unitaires du pipeline de traduction (M-09, §9) — sans base ni réseau.

Vérifient les briques pures : extraction des seuls segments textuels (jamais un
champ structuré), réinjection avec repli sur la source, comptabilité du coût.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # racine backend/

from enrich import translate  # noqa: E402

# Schéma mêlant tous les types + un groupe répétable
SCHEMA = {
    "fields": [
        {"key": "intro", "type": "textarea"},
        {"key": "note", "type": "text"},
        {"key": "hour", "type": "time"},
        {"key": "flag", "type": "bool"},
        {"key": "count", "type": "number"},
        {"key": "site", "type": "url"},
        {"key": "tel", "type": "phone"},
        {"key": "kind", "type": "select"},
    ],
    "repeat": {"key": "items", "fields": [
        {"key": "label", "type": "text"},
        {"key": "qty", "type": "number"},
    ]},
}

CONTENT = {
    "intro": "Bienvenue chez nous",
    "note": "Clés dans la boîte",
    "hour": "16:00",
    "flag": True,
    "count": 4,
    "site": "https://exemple.test",
    "tel": "+34 600 000 000",
    "kind": "private",
    "items": [{"label": "Serviettes", "qty": 2},
              {"label": "", "qty": 1},
              "pas un dict"],
}


def test_collect_only_text_fields():
    texts = translate.collect_section_texts(SCHEMA, CONTENT, "Corps **libre**")
    # Uniquement text/textarea (+ body) ; jamais time/bool/number/url/phone/select
    assert set(texts) == {"f:intro", "f:note", "r:items:0:label", "body"}
    assert texts["f:intro"] == "Bienvenue chez nous"
    assert texts["body"] == "Corps **libre**"


def test_collect_includes_title_override():
    """V2-42 : le titre personnalisé voyage sous la référence dédiée `title` (motif
    de `body`) ; vide/blanc → jamais collecté."""
    texts = translate.collect_section_texts(SCHEMA, {}, None, "Piscine & barbecue")
    assert texts == {"title": "Piscine & barbecue"}
    assert "title" not in translate.collect_section_texts(SCHEMA, {}, None, "   ")
    assert "title" not in translate.collect_section_texts(SCHEMA, {}, None, None)
    # Les valeurs structurées ne sont jamais collectées
    for ref in texts:
        assert "hour" not in ref and "site" not in ref and "tel" not in ref
        assert "kind" not in ref and "count" not in ref


def test_apply_preserves_structured_and_falls_back():
    # Traduction partielle : intro traduit, note absente (repli source)
    tr = {"f:intro": "Welcome", "r:items:0:label": "Towels", "body": "Free text",
          "title": "Pool & barbecue"}
    content2, body2, title2 = translate.apply_section_texts(SCHEMA, CONTENT, "Corps", tr)
    assert title2 == "Pool & barbecue"             # titre traduit ressort (V2-42)
    assert content2["intro"] == "Welcome"          # traduit
    assert content2["note"] == "Clés dans la boîte"  # non traduit → source (repli)
    assert content2["hour"] == "16:00"             # structuré intact
    assert content2["flag"] is True and content2["count"] == 4
    assert content2["site"] == "https://exemple.test"
    assert content2["kind"] == "private"
    assert content2["items"][0]["label"] == "Towels"
    assert content2["items"][0]["qty"] == 2        # nombre intact
    assert content2["items"][1]["label"] == ""     # vide inchangé
    assert body2 == "Free text"


def test_apply_without_body_translation_returns_none():
    _c, body2, title2 = translate.apply_section_texts(SCHEMA, CONTENT, "Corps", {"f:note": "X"})
    assert body2 is None                            # body non traduit → repli au rendu
    assert title2 is None                           # titre non traduit → repli source au rendu


def test_claude_translator_parses_and_costs():
    from types import SimpleNamespace

    class _Msgs:
        def create(self, *, model, max_tokens, messages):
            payload = '{"1": "Hello", "2": "World"}'
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=payload)],
                usage=SimpleNamespace(input_tokens=100, output_tokens=50))

    class _AI:
        messages = _Msgs()

    t = translate.ClaudeTranslator(_AI())
    out, meta = t.translate({"1": "Bonjour", "2": "Monde"},
                            target_lang="en", source_lang="fr")
    assert out == {"1": "Hello", "2": "World"}
    assert meta["units"] == 150 and meta["cost_cts"] > 0


def test_claude_translator_drops_unknown_keys():
    from types import SimpleNamespace

    class _Msgs:
        def create(self, *, model, max_tokens, messages):
            # L'IA invente une clé et en oublie une : on ne garde que le valide
            payload = '{"1": "Hello", "99": "Intruder", "2": ""}'
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=payload)],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    class _AI:
        messages = _Msgs()

    out, _ = translate.ClaudeTranslator(_AI()).translate(
        {"1": "Bonjour", "2": "Monde"}, target_lang="en", source_lang="fr")
    assert out == {"1": "Hello"}                    # clé inconnue + vide écartées


def test_empty_batch_no_call():
    class _AI:
        def __getattr__(self, name):  # ne doit jamais être sollicité
            raise AssertionError("aucun appel attendu pour un lot vide")

    out, meta = translate.ClaudeTranslator(_AI()).translate(
        {}, target_lang="en", source_lang="fr")
    assert out == {} and meta["units"] == 0


# ── V2-70 : robustesse des gros guides / réponses tronquées ───────────────────

def test_parse_translations_strict_and_tolerant():
    """JSON strict d'abord ; une réponse TRONQUÉE livre ses paires COMPLÈTES (jette la
    dernière coupée) au lieu de tout perdre ; échappements préservés ; non-chaînes ignorées."""
    assert translate._parse_translations('{"1":"x","2":"y"}') == {"1": "x", "2": "y"}
    # tronqué au milieu de la 2e valeur → seule la 1re paire est récupérée
    assert translate._parse_translations('{"1": "Hello", "2": "Wor') == {"1": "Hello"}
    # guillemets échappés préservés, dernière valeur non fermée
    assert translate._parse_translations('{"1": "a \\"b\\" c", "2": "z"') \
        == {"1": 'a "b" c', "2": "z"}
    # valeur non-chaîne (jamais une traduction) écartée
    assert translate._parse_translations('{"1":"x","2":3}') == {"1": "x"}


def _fake_ai(handler):
    """Client factice : `handler(keys, payload)` → dict de traductions à émettre."""
    from types import SimpleNamespace
    import json as _j
    calls: list[list] = []

    class _Msgs:
        def create(self, *, model, max_tokens, messages):
            payload = _j.loads(messages[0]["content"].rsplit("Objet à traduire :", 1)[-1].strip())
            keys = list(payload)
            calls.append(keys)
            emit = handler(keys, payload)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=_j.dumps(emit, ensure_ascii=False))],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    class _AI:
        messages = _Msgs()

    return _AI(), calls


def test_translator_batches_bounded():
    """V2-70 : la traduction est DÉCOUPÉE en lots bornés (`translate_batch_size`)."""
    import enrich.translate as tr
    old = tr.settings.translate_batch_size
    tr.settings.translate_batch_size = 2
    try:
        ai, calls = _fake_ai(lambda keys, payload: {k: "T:" + payload[k] for k in keys})
        texts = {str(i): f"v{i}" for i in range(1, 6)}     # 5 → lots [2, 2, 1]
        out, meta = tr.ClaudeTranslator(ai).translate(texts, target_lang="en", source_lang="fr")
        assert out == {str(i): f"T:v{i}" for i in range(1, 6)}
        assert len(calls) == 3 and [len(c) for c in calls] == [2, 2, 1]
    finally:
        tr.settings.translate_batch_size = old


def test_translator_retries_missing_after_truncation():
    """V2-70 : un lot tronqué (clé manquante) est re-tenté sur les SEULES clés manquantes
    → tout finit traduit, un lot fautif n'emporte pas les autres."""
    import enrich.translate as tr
    old = tr.settings.translate_batch_size
    tr.settings.translate_batch_size = 3
    try:
        def handler(keys, payload):
            emit = {k: "T:" + payload[k] for k in keys}
            if len(keys) > 1:                # 1er passage d'un lot : omet la dernière clé
                emit.pop(keys[-1])
            return emit
        ai, calls = _fake_ai(handler)
        texts = {str(i): f"v{i}" for i in range(1, 4)}     # 1 lot de 3
        out, _ = tr.ClaudeTranslator(ai).translate(texts, target_lang="en", source_lang="fr")
        assert out == {"1": "T:v1", "2": "T:v2", "3": "T:v3"}   # récupéré via re-tentative
        assert len(calls) == 2                                  # lot + re-tentative du manquant
    finally:
        tr.settings.translate_batch_size = old
