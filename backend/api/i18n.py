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
#   email.*          — libellés des emails d'envoi du guide (emails._EMAIL, V2-23d)
#   chapter.*        — noms de chapitres (_CHAPTER_NAMES)
#   chapter_tab.*    — noms de chapitre spécialisés par onglet (_CHAPTER_TAB_NAMES)
#   option.*         — libellés de valeurs de select (_OPTION_LABELS)
#   cuisine.*        — libellés de cuisines (_CUISINE_LABELS)
#   section.*.name / .description — sections du seed (section_templates)
#   poi_category.*   — catégories de POI du seed (poi_categories.name_i18n)


def ui_key(k: str) -> str:
    return f"ui.{k}"


def email_key(k: str) -> str:
    """Clé d'un libellé d'email LOCALISÉ (envoi du guide, V2-23d). Domaine `email.*`
    superposable via `ui_translations` pour les langues supplémentaires (repli FR)."""
    return f"email.{k}"


def chapter_key(ch: str) -> str:
    return f"chapter.{ch}"


def chapter_tab_key(ch: str, tab: str) -> str:
    return f"chapter_tab.{ch}.{tab}"


def option_key(value: str) -> str:
    return f"option.{value}"


def field_label_key(section_code: str, field_key: str) -> str:
    """Clé d'un libellé de champ simple porté par `field_schema.fields[].label`
    (rendu dans le guide voyageur). Scopée par section : un même `key` (`notes`,
    `details`…) porte un libellé différent selon la section — pas de collision."""
    return f"field.{section_code}.{field_key}"


def repeat_field_label_key(section_code: str, repeat_key: str, field_key: str) -> str:
    """Clé d'un libellé de champ d'un groupe répétable
    (`field_schema.repeat.fields[].label`, ex. « Équipement », « Tâche »). Le
    segment `repeat_key` la distingue d'un champ simple de même nom."""
    return f"field.{section_code}.{repeat_key}.{field_key}"


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

# Langues portées par le CODE et le SEED (colonnes source de l'inventaire). Les
# AUTRES langues du registre (nl/de/it/sq…) vivent dans `ui_translations` et sont
# superposées au rendu (overlay). Sert à décider quelles langues sont candidates à
# la génération de propositions (volet 3) : jamais une source.
SOURCE_LANGS = ("fr", "en", "es")
_SOURCE_LANGS = SOURCE_LANGS  # alias interne historique


def _entry(key: str, context: str, i18n: dict[str, Any] | None) -> dict[str, str]:
    d = i18n or {}
    return {"key": key, "context": context,
            **{lg: (d.get(lg) or "") for lg in _SOURCE_LANGS}}


def _collect_field_schema_labels(entries: dict[str, dict[str, str]],
                                 code: str, schema: Any) -> None:
    """Recense les libellés d'un `field_schema` de section RENDUS dans le guide
    voyageur (`guide_page._render_fields`), avec les MÊMES clés que le SSR :

      * `field.<code>.<key>`             — libellé d'un champ simple ;
      * `field.<code>.<repeat>.<key>`    — libellé d'un champ de groupe répétable ;
      * `option.<valeur>`                — valeur de `select` non déjà couverte par
        `_OPTION_LABELS` (le libellé affiché reste celui du code quand il existe).

    Mute `entries` en place. Ne recense jamais les types structurés eux-mêmes
    (heure, booléen, URL…) : seuls les *libellés* sont traduits, pas les valeurs."""
    schema = schema or {}

    def _put(e: dict[str, str]) -> None:
        entries[e["key"]] = e

    for f in schema.get("fields", []):
        fk = f.get("key")
        if not fk:
            continue
        _put(_entry(field_label_key(code, fk),
                    f"Libellé du champ « {fk} » (section {code})", f.get("label")))
        # Valeurs de `select` : le libellé lisible vient de `_OPTION_LABELS`
        # (recensé en étape 4). On n'ajoute une entrée que pour une valeur NON
        # couverte (jamais d'écrasement d'un libellé de code par du vide).
        if f.get("type") == "select":
            for val in f.get("options") or []:
                ok = option_key(str(val))
                if ok not in entries:
                    _put(_entry(ok, f"Valeur de champ « {val} »", None))

    repeat = schema.get("repeat")
    if repeat:
        rk = repeat.get("key") or ""
        for rf in repeat.get("fields", []):
            rfk = rf.get("key")
            if not rfk:
                continue
            _put(_entry(repeat_field_label_key(code, rk, rfk),
                        f"Libellé du champ « {rfk} » du groupe « {rk} » "
                        f"(section {code})", rf.get("label")))


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

    # 1bis. Libellés des emails d'envoi du guide (V2-23d) : localisés vers le
    #       voyageur/prospect (contrairement aux emails owner restés FR).
    from . import emails as em  # import différé (pas de cycle : emails → i18n)
    for k in em._EMAIL["fr"]:
        i18n = {lg: em._EMAIL.get(lg, {}).get(k) for lg in _SOURCE_LANGS}
        add(_entry(email_key(k), "Email d'envoi du guide", i18n))

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

    # 6. Sections du seed : nom affiché dans le guide, description éditeur, et les
    #    LIBELLÉS DU field_schema (labels de champs, options de select, labels de
    #    groupes répétables) — tous rendus dans le guide voyageur, donc traduisibles
    #    au même titre que les noms de sections (sinon : repli FR visible dans une
    #    langue supplémentaire, guide à moitié traduit).
    rows = conn.execute(
        "SELECT code, name_i18n, description_i18n, field_schema FROM section_templates "
        "ORDER BY chapter, sort_order, code").fetchall()
    for r in rows:
        add(_entry(section_name_key(r["code"]),
                   f"Nom de la section « {r['code']} »", r["name_i18n"]))
        add(_entry(section_desc_key(r["code"]),
                   f"Description de la section « {r['code']} » (éditeur)",
                   r["description_i18n"]))
        _collect_field_schema_labels(entries, r["code"], r["field_schema"])

    # 7. Catégories de POI du seed (nom affiché dans le guide).
    cats = conn.execute(
        "SELECT code, name_i18n FROM poi_categories ORDER BY chapter, code"
    ).fetchall()
    for r in cats:
        add(_entry(poi_category_key(r["code"]),
                   f"Catégorie de lieu « {r['code']} »", r["name_i18n"]))

    return dict(sorted(entries.items()))
