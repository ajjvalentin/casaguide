"""Génération des PROPOSITIONS de traduction des libellés STATIQUES du guide
voyageur (V2-21a, volet 3).

Le volet 2 a posé l'inventaire (`i18n/inventory.json`), le stockage
(`ui_translations`) et l'outillage export/réimport ; il restait à *remplir* la
colonne « proposition ». Ce module génère ces propositions avec Claude, **une
langue à la fois**, en imposant au modèle le REGISTRE tranché pour la langue
(`languages.register_note` — vouvoiement, forme de politesse…). Les propositions
atterrissent dans `ui_translations` (source unique, invariant 15) : l'export
relecteur les montre en colonne « proposition », le relecteur corrige, le
réimport écrase. Une correction déjà importée n'est jamais clobbée (le
générateur saute par défaut les clés déjà présentes ; `overwrite` pour forcer).

Ce module NE touche PAS la base : il fournit le cœur pur (sélection des libellés,
découpage en lots) et le traducteur Claude (DB-agnostique, comme
`translate.ClaudeTranslator`). L'orchestration + écriture vivent dans
`ops/i18n_generate.py`. Le traducteur est **injectable** (`translator=`) pour
tester sans réseau.
"""
from __future__ import annotations

import json
from typing import Iterator

import anthropic

from .settings import settings

# Noms de langue pour l'invite (le modèle traduit du français vers ces langues).
_LANG_NAMES = {
    "en": "anglais", "es": "espagnol", "fr": "français", "de": "allemand",
    "nl": "néerlandais", "it": "italien", "sq": "albanais", "pt": "portugais",
    "ca": "catalan",
}


def select_labels(inventory: dict, existing: dict[str, str] | None = None,
                  *, overwrite: bool = False) -> dict[str, str]:
    """`{clé: source_fr}` à (re)traduire, depuis l'inventaire.

    - ignore les clés à source FR vide (rien à traduire) ;
    - par défaut, saute les clés **déjà présentes** en base (`existing`) → ne
      clobbe jamais une correction importée. `overwrite=True` les régénère.

    Fonction PURE (cœur testable sans réseau ni base)."""
    existing = existing or {}
    out: dict[str, str] = {}
    for key, entry in inventory.items():
        fr = (entry.get("fr") or "").strip()
        if not fr:
            continue
        if not overwrite and key in existing:
            continue
        out[key] = fr
    return out


def iter_batches(labels: dict[str, str], size: int) -> Iterator[dict[str, str]]:
    """Découpe `{clé: texte}` en lots de `size` clés (ordre stable). Bornes une
    invite Claude et un JSON de réponse à une taille raisonnable."""
    keys = list(labels)
    size = max(1, size)
    for i in range(0, len(keys), size):
        chunk = keys[i:i + size]
        yield {k: labels[k] for k in chunk}


_PROMPT = """\
Tu traduis les libellés d'INTERFACE d'un guide d'accueil numérique pour un
logement de vacances, du français vers le {dst}. Ce sont des libellés courts :
titres de sections, noms de champs de formulaire, boutons, noms de catégories de
lieux, types de cuisine.

REGISTRE IMPOSÉ pour cette langue (à respecter partout) : {register}

RÈGLES STRICTES :
- Traduis chaque VALEUR de l'objet JSON ci-dessous en conservant EXACTEMENT les
  mêmes clés.
- Ce sont des libellés d'interface : garde-les COURTS et naturels, cohérents
  entre eux ; jamais une phrase là où le français est un simple libellé.
- La clé (identifiant pointé, ex. `field.A_parking.parking_type`,
  `chapter.B`, `cuisine.italian`) te donne le domaine : sers-t'en pour
  désambiguïser un mot polysémique. Ne la traduis pas, ne la renvoie pas modifiée.
- Ne traduis pas les noms propres, marques, codes. Conserve la ponctuation utile.
- N'ajoute, ne supprime, ne fusionne aucune clé. N'invente aucun contenu.
- Réponds UNIQUEMENT avec l'objet JSON traduit, sans texte ni ``` autour.

Objet à traduire (clé → libellé français) :
{payload}
"""


def build_prompt(labels: dict[str, str], *, target_lang: str,
                 register_note: str | None) -> str:
    """Invite de traduction d'un lot (pure — testable)."""
    register = (register_note or "").strip() or (
        "vouvoiement — le guide s'adresse poliment au voyageur.")
    return _PROMPT.format(
        dst=_LANG_NAMES.get(target_lang, target_lang),
        register=register,
        payload=json.dumps(labels, ensure_ascii=False, indent=1))


class ClaudeUILabelTranslator:
    """Traducteur des libellés statiques par l'API Claude (modèle
    `settings.translate_model`, comme le pipeline de contenu M-09)."""

    def __init__(self, client: anthropic.Anthropic):
        self.client = client

    def translate(self, labels: dict[str, str], *, target_lang: str,
                  register_note: str | None = None) -> tuple[dict[str, str], dict]:
        """Traduit `{clé: libellé_fr}` → (`{clé: libellé traduit}`, méta
        `{units, cost_cts}`). Ne renvoie que les clés effectivement traduites
        (chaînes non vides) qui figuraient dans l'entrée : une clé manquante
        retombera sur le FR au rendu (jamais de trou, invariant 15)."""
        if not labels:
            return {}, {"units": 0, "cost_cts": 0.0}
        prompt = build_prompt(labels, target_lang=target_lang,
                              register_note=register_note)
        msg = self.client.messages.create(
            model=settings.translate_model,
            max_tokens=settings.translate_max_tokens,
            messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)  # non-JSON -> ValueError -> l'appelant échoue proprement
        result = {k: v for k, v in data.items()
                  if k in labels and isinstance(v, str) and v.strip()}
        inp, out = settings.model_prices_usd.get(settings.translate_model, (1.0, 5.0))
        usd = msg.usage.input_tokens / 1e6 * inp + msg.usage.output_tokens / 1e6 * out
        meta = {"units": msg.usage.input_tokens + msg.usage.output_tokens,
                "cost_cts": round(usd * settings.usd_to_eur * 100, 4)}
        return result, meta
