"""Socle i18n des libellés STATIQUES (V2-21a, volet 2).

Deux responsabilités, volontairement dans un module **feuille** (aucun import de
`guide_page` au niveau module → pas de cycle) :

1. **Les clés stables** de l'inventaire. Chaque libellé statique du guide voyageur
   porte une clé stable (`ui.eyebrow`, `chapter.A`, `section.A_arrival.name`…).
   Ces clés sont construites par les helpers ci-dessous et utilisées AUX DEUX
   BOUTS — par le collecteur d'inventaire (`collect_inventory`) et par le rendu
   SSR (`guide_page` superpose l'overlay via ces mêmes clés). Une seule source de
   vérité de la convention de nommage → aucune dérive possible.

2. **L'overlay de rendu**. Pour une langue publiée supplémentaire (nl/de/it/sq…),
   les libellés traduits vivent en base (`ui_translations`, importés depuis la
   relecture). Le SSR charge la carte `{clé: texte}` de la langue effective et la
   pose dans un `ContextVar` le temps du rendu ; `guide_page._t` & co la
   consultent en priorité, puis retombent sur le code (FR/EN/ES) et enfin le FR.
   Pour FR/EN/ES l'overlay est **vide** (jamais importées) → rendu identique
   (invariant de non-régression du volet).
"""
from __future__ import annotations

import contextvars
from typing import Any

# ── Clés stables de l'inventaire ─────────────────────────────────────────────
# Convention : <domaine>.<identifiant>[.<champ>]. Domaines :
#   ui.*             — libellés d'interface du guide (guide_page._UI)
#   chapter.*        — noms de chapitres (_CHAPTER_NAMES)
#   chapter_tab.*    — noms de chapitre spécialisés par onglet (_CHAPTER_TAB_NAMES)
#   option.*         — libellés de valeurs de select (_OPTION_LABELS)
#   cuisine.*        — libellés de cuisines (_CUISINE_LABELS)
#   section.*.name / .description — sections du seed (section_templates)
#   poi_category.*   — catégories de POI du seed (poi_categories.name_i18n)


def ui_key(k: str) -> str:
    return f"ui.{k}"


def chapter_key(ch: str) -> str:
    return f"chapter.{ch}"


def chapter_tab_key(ch: str, tab: str) -> str:
    return f"chapter_tab.{ch}.{tab}"


def option_key(value: str) -> str:
    return f"option.{value}"


def cuisine_key(value: str) -> str:
    return f"cuisine.{value}"


def section_name_key(code: str) -> str:
    return f"section.{code}.name"


def section_desc_key(code: str) -> str:
    return f"section.{code}.description"


def poi_category_key(code: str) -> str:
    return f"poi_category.{code}"


# ── Overlay de rendu (ContextVar par requête) ────────────────────────────────
# {clé_inventaire: texte} pour la LANGUE EFFECTIVE du rendu en cours. Vide par
# défaut (FR/EN/ES ou aucune traduction supplémentaire → rendu depuis le code).
_OVERLAY: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "ui_overlay", default={})


def current_overlay() -> dict[str, str]:
    """L'overlay de la requête courante (jamais None)."""
    return _OVERLAY.get()


def set_overlay(mapping: dict[str, str] | None) -> contextvars.Token:
    """Pose l'overlay pour le rendu courant ; renvoie le jeton de restauration.
    À restaurer via `reset_overlay` (guide_page le fait dans un `finally`)."""
    return _OVERLAY.set(mapping or {})


def reset_overlay(token: contextvars.Token) -> None:
    _OVERLAY.reset(token)


def overlaid(key: str) -> str | None:
    """Traduction superposée pour `key`, ou None si absente (repli au code)."""
    return current_overlay().get(key) or None


# ── Collecte de l'inventaire depuis les sources vivantes ─────────────────────
# Chaque entrée : {key, context, fr, en, es}. `context` (FR) aide le relecteur.
# Les libellés de CODE viennent de `guide_page` (importé DANS la fonction, pas au
# niveau module → pas de cycle) ; les libellés de SEED viennent de la base.

_SOURCE_LANGS = ("fr", "en", "es")


def _entry(key: str, context: str, i18n: dict[str, Any] | None) -> dict[str, str]:
    d = i18n or {}
    return {"key": key, "context": context,
            **{lg: (d.get(lg) or "") for lg in _SOURCE_LANGS}}


def collect_inventory(conn) -> dict[str, dict[str, str]]:
    """Recense TOUS les libellés statiques du guide voyageur, depuis leurs sources
    vivantes (code `guide_page` + seed en base). Renvoie un dict clé→entrée, trié
    par clé. Rejouable : c'est le socle de `ops/i18n_inventory.py` (diff des clés
    nouvelles/disparues) et de l'export/réimport."""
    from . import guide_page as gp  # import différé (module feuille, pas de cycle)

    entries: dict[str, dict[str, str]] = {}

    def add(e: dict[str, str]) -> None:
        entries[e["key"]] = e

    # 1. Libellés d'interface du guide (_UI). La source FR liste toutes les clés.
    for k in gp._UI["fr"]:
        i18n = {lg: gp._UI.get(lg, {}).get(k) for lg in _SOURCE_LANGS}
        add(_entry(ui_key(k), "Guide voyageur — libellé d'interface", i18n))

    # 2. Noms de chapitres.
    for ch in gp._CHAPTER_NAMES["fr"]:
        i18n = {lg: gp._CHAPTER_NAMES.get(lg, {}).get(ch) for lg in _SOURCE_LANGS}
        add(_entry(chapter_key(ch), f"Nom du chapitre {ch}", i18n))

    # 3. Noms de chapitre spécialisés par onglet (C/home, C/around…).
    for (ch, tab), i18n in gp._CHAPTER_TAB_NAMES.items():
        add(_entry(chapter_tab_key(ch, tab),
                   f"Chapitre {ch} (onglet « {tab} »)", i18n))

    # 4. Libellés de valeurs de select (type de stationnement…).
    for value, i18n in gp._OPTION_LABELS.items():
        add(_entry(option_key(value), f"Valeur de champ « {value} »", i18n))

    # 5. Libellés de cuisines.
    for value, i18n in gp._CUISINE_LABELS.items():
        add(_entry(cuisine_key(value), f"Type de cuisine « {value} »", i18n))

    # 6. Sections du seed (nom affiché dans le guide + description éditeur).
    rows = conn.execute(
        "SELECT code, name_i18n, description_i18n FROM section_templates "
        "ORDER BY chapter, sort_order, code").fetchall()
    for r in rows:
        add(_entry(section_name_key(r["code"]),
                   f"Nom de la section « {r['code']} »", r["name_i18n"]))
        add(_entry(section_desc_key(r["code"]),
                   f"Description de la section « {r['code']} » (éditeur)",
                   r["description_i18n"]))

    # 7. Catégories de POI du seed (nom affiché dans le guide).
    cats = conn.execute(
        "SELECT code, name_i18n FROM poi_categories ORDER BY chapter, code"
    ).fetchall()
    for r in cats:
        add(_entry(poi_category_key(r["code"]),
                   f"Catégorie de lieu « {r['code']} »", r["name_i18n"]))

    return dict(sorted(entries.items()))
