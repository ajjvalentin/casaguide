"""Pipeline de traduction du guide voyageur (M-09, §9 du CdC).

Principe §9 : les traductions sont **générées puis stockées** (jamais de
traduction à la volée côté voyageur — invariant 4). La langue source est
`properties.default_lang` (fr par défaut) ; les langues cibles du MVP sont
`en` et `es` (`settings.translate_langs`). DE/NL et la relecture propriétaire
sont hors périmètre (V2).

Ce qui est traduit :
  * le contenu **textuel** des sections voyageur (audience='guest') : champs
    `text`/`textarea` du `content` JSONB (y compris les groupes répétables) et
    le `body_md` ;
  * les descriptions et « coups de cœur » des POI **retenus** (approved/edited).

Ce qui n'est **jamais** traduit : les noms propres de POI, les valeurs
structurées non textuelles (heures, booléens, nombres, URLs, téléphones, clés
de `select`), et les secrets (wifi, boîte à clés — ils ne transitent jamais par
ici). Les libellés fixes (noms de sections/catégories, boutons) sont traduits
côté rendu via les `name_i18n` du seed et un dictionnaire statique.

`is_stale` : toute sauvegarde de section ou édition de POI marque ses
traductions périmées (côté API). La re-traduction ne retraite **que** le
manquant ou le périmé (ciblage). Chaque appel Claude est comptabilisé dans
`api_costs` (operation='translate').

Le traducteur est **injectable** (`translator=`) pour tester sans réseau.
"""
from __future__ import annotations

import copy
import json

import anthropic

from . import db
from .settings import settings

# Seuls ces types de champ portent du texte libre à traduire. Les autres
# (time, bool, number, url, phone, select) sont structurés : jamais traduits.
TRANSLATABLE_TYPES = {"text", "textarea"}


# ── Extraction / réinjection du texte traduisible d'une section ──────────────

def collect_section_texts(schema: dict, content: dict,
                          body_md: str | None) -> dict[str, str]:
    """Segments traduisibles d'une section, indexés par une référence stable.

    Références : `f:<key>` (champ simple), `r:<rkey>:<i>:<key>` (champ d'un item
    de groupe répétable), `body` (texte libre). Seules les chaînes non vides des
    champs `text`/`textarea` sont retenues."""
    out: dict[str, str] = {}
    content = content or {}
    for f in schema.get("fields", []):
        if f.get("type") in TRANSLATABLE_TYPES:
            v = content.get(f.get("key"))
            if isinstance(v, str) and v.strip():
                out[f"f:{f['key']}"] = v
    repeat = schema.get("repeat")
    if repeat:
        rkey = repeat.get("key")
        rtypes = {rf.get("key"): rf.get("type") for rf in repeat.get("fields", [])}
        for i, item in enumerate(content.get(rkey) or []):
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                if rtypes.get(k) in TRANSLATABLE_TYPES and isinstance(v, str) and v.strip():
                    out[f"r:{rkey}:{i}:{k}"] = v
    if body_md and body_md.strip():
        out["body"] = body_md
    return out


def apply_section_texts(schema: dict, content: dict, body_md: str | None,
                        tr: dict[str, str]) -> tuple[dict, str | None]:
    """Reconstruit (content, body_md) traduits : copie de la source dont seuls les
    segments présents dans `tr` sont remplacés. Les champs structurés et les
    segments non traduits restent tels quels (repli élégant sur la source)."""
    tcontent = copy.deepcopy(content or {})
    for f in schema.get("fields", []):
        ref = f"f:{f.get('key')}"
        if ref in tr:
            tcontent[f["key"]] = tr[ref]
    repeat = schema.get("repeat")
    if repeat:
        rkey = repeat.get("key")
        arr = tcontent.get(rkey)
        if isinstance(arr, list):
            for i, item in enumerate(arr):
                if not isinstance(item, dict):
                    continue
                for k in list(item.keys()):
                    ref = f"r:{rkey}:{i}:{k}"
                    if ref in tr:
                        item[k] = tr[ref]
    tbody = tr.get("body") if "body" in tr else None
    return tcontent, tbody


# ── Traducteur Claude (JSON strict, coût comptabilisé) ───────────────────────

_LANG_NAMES = {"en": "anglais", "es": "espagnol", "fr": "français",
               "de": "allemand", "nl": "néerlandais"}

_PROMPT = """\
Tu es un traducteur professionnel pour un guide d'accueil de logement de
vacances. Traduis du {src} vers le {dst} chacune des valeurs de l'objet JSON
ci-dessous, en conservant EXACTEMENT les mêmes clés.

RÈGLES STRICTES :
- Traduis uniquement les valeurs textuelles ; garde le sens, le ton courtois et
  la mise en forme Markdown (gras `**…**`, listes `- `, retours à la ligne).
- Ne traduis PAS les noms propres, marques, SSID wifi, URLs, adresses e-mail,
  numéros. Garde-les à l'identique.
- N'ajoute, ne supprime, ne fusionne aucune clé. N'invente aucun contenu.
- Réponds UNIQUEMENT avec l'objet JSON traduit, sans markdown ni commentaire.

Objet à traduire :
{payload}
"""


class ClaudeTranslator:
    """Traducteur par l'API Claude (modèle `settings.translate_model`)."""

    def __init__(self, client: anthropic.Anthropic):
        self.client = client

    def translate(self, texts: dict[str, str], *, target_lang: str,
                  source_lang: str) -> tuple[dict[str, str], dict]:
        """Traduit {clé: texte} → ({clé: texte traduit}, méta {units, cost_cts}).

        Ne renvoie que les clés effectivement traduites (chaînes) : une clé
        manquante retombera sur la source au rendu (jamais de trou)."""
        if not texts:
            return {}, {"units": 0, "cost_cts": 0.0}
        prompt = _PROMPT.format(
            src=_LANG_NAMES.get(source_lang, source_lang),
            dst=_LANG_NAMES.get(target_lang, target_lang),
            payload=json.dumps(texts, ensure_ascii=False, indent=1))
        msg = self.client.messages.create(
            model=settings.translate_model,
            max_tokens=settings.translate_max_tokens,
            messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)  # non-JSON -> ValueError -> job 'failed', rien de corrompu
        result = {k: v for k, v in data.items()
                  if k in texts and isinstance(v, str) and v.strip()}
        inp, out = settings.model_prices_usd.get(settings.translate_model, (1.0, 5.0))
        usd = msg.usage.input_tokens / 1e6 * inp + msg.usage.output_tokens / 1e6 * out
        meta = {"units": msg.usage.input_tokens + msg.usage.output_tokens,
                "cost_cts": round(usd * settings.usd_to_eur * 100, 4)}
        return result, meta


# ── Orchestrateur ────────────────────────────────────────────────────────────

def run(property_id: str, *, target_langs: list[str] | None = None,
        job_id: str | None = None, translator=None,
        anthropic_client: anthropic.Anthropic | None = None) -> dict:
    """(Re)traduit ce qui manque ou est périmé pour un logement, puis publie la
    liste des langues disponibles (`properties.published_langs`).

    `translator` (objet exposant `.translate(texts, target_lang, source_lang)`)
    est injectable pour les tests ; à défaut, un `ClaudeTranslator` est construit
    à partir de `anthropic_client` (ou d'un client réel). Tracé dans
    `enrichment_jobs` (trigger='translate'), coûts dans `api_costs`."""
    summary = {"langs": {}, "cost_cts": 0.0, "translated": 0}

    with db.connect() as conn:
        prop = db.load_property(conn, property_id)
        source_lang = prop.get("default_lang") or "fr"
        langs = [l for l in (target_langs or settings.translate_langs)
                 if l and l != source_lang]

        if job_id is None:
            job_id = db.job_start(conn, property_id, "translate")
        else:
            db.job_mark_running(conn, job_id)
        conn.commit()

        try:
            if translator is None:
                import os
                ai = anthropic_client or anthropic.Anthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"])
                translator = ClaudeTranslator(ai)

            sections = db.translatable_sections(conn, property_id)
            pois = db.translatable_pois(conn, property_id)

            for lang in langs:
                n = _translate_lang(conn, property_id, job_id, lang, source_lang,
                                    sections, pois, translator, summary)
                summary["langs"][lang] = n
                conn.commit()

            # Publie la liste des langues cibles (les libellés fixes et les
            # name_i18n du seed sont localisés même sans texte propriétaire).
            db.set_published_langs(conn, property_id, langs)
            db.job_finish(conn, job_id, "done")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            db.job_finish(conn, job_id, "failed",
                          error=f"{type(exc).__name__}: {exc}")
            conn.commit()
            raise

    summary["job_id"] = job_id
    return summary


def _translate_lang(conn, property_id, job_id, lang, source_lang,
                    sections, pois, translator, summary) -> int:
    """Traduit vers `lang` uniquement le manquant/périmé. Retourne le nombre
    d'éléments (sections + POI) (re)traduits."""
    batch: dict[str, str] = {}
    keymap: dict[str, tuple] = {}     # clé opaque -> ("section"/"poi", id, ref)
    pending_sections: dict[str, dict] = {}   # section_id -> {schema, content, body_md}
    pending_pois: set[str] = set()
    counter = 0

    def _add(text: str, target: tuple) -> None:
        nonlocal counter
        counter += 1
        key = str(counter)
        batch[key] = text
        keymap[key] = target

    for sec in sections:
        sid = str(sec["section_id"])
        row = db.get_section_translation(conn, sid, lang)
        if row is not None and not row["is_stale"]:
            continue  # à jour : rien à faire (ciblage)
        schema = sec.get("field_schema") or {}
        texts = collect_section_texts(schema, sec.get("content") or {},
                                      sec.get("body_md"))
        if not texts:
            if row is not None:
                db.delete_section_translation(conn, sid, lang)  # texte retiré
            continue
        pending_sections[sid] = {"schema": schema, "content": sec.get("content") or {},
                                 "body_md": sec.get("body_md")}
        for ref, text in texts.items():
            _add(text, ("section", sid, ref))

    for poi in pois:
        pid = str(poi["id"])
        row = db.get_poi_translation(conn, pid, lang)
        if row is not None and not row["is_stale"]:
            continue
        fields = {k: poi.get(k) for k in ("description_md", "owner_comment")
                  if isinstance(poi.get(k), str) and poi.get(k).strip()}
        if not fields:
            if row is not None:
                db.delete_poi_translation(conn, pid, lang)
            continue
        pending_pois.add(pid)
        for ref, text in fields.items():
            _add(text, ("poi", pid, ref))

    if not batch:
        return 0

    translations, meta = translator.translate(
        batch, target_lang=lang, source_lang=source_lang)
    db.record_cost(conn, property_id, job_id, "anthropic", "translate",
                   meta["units"], meta["cost_cts"])
    summary["cost_cts"] += meta["cost_cts"]

    # Regroupe les segments traduits par section / POI
    sec_tr: dict[str, dict] = {sid: {} for sid in pending_sections}
    poi_tr: dict[str, dict] = {pid: {} for pid in pending_pois}
    for key, translated in translations.items():
        kind, oid, ref = keymap[key]
        (sec_tr if kind == "section" else poi_tr)[oid][ref] = translated

    for sid, meta_sec in pending_sections.items():
        content2, body2 = apply_section_texts(
            meta_sec["schema"], meta_sec["content"], meta_sec["body_md"],
            sec_tr[sid])
        db.upsert_section_translation(conn, sid, lang, content2, body2)

    for pid in pending_pois:
        tr = poi_tr[pid]
        db.upsert_poi_translation(conn, pid, lang,
                                  tr.get("description_md"), tr.get("owner_comment"))

    n = len(pending_sections) + len(pending_pois)
    summary["translated"] += n
    return n
