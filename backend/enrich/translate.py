"""Pipeline de traduction du guide voyageur (M-09, §9 du CdC).

Principe §9 : les traductions sont **générées puis stockées** (jamais de
traduction à la volée côté voyageur — invariant 4). La langue source est
`properties.default_lang` (fr par défaut) ; les langues cibles viennent du
REGISTRE des langues (V2-21a) — les langues `published` du produit, hors langue
source (jamais une liste en dur). L'appelant API les impose déjà (plafonnées par
le plan) ; en usage CLI, le repli lit le registre (`db.published_language_codes`).

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
import logging
import re

import anthropic

from . import db
from .settings import settings

log = logging.getLogger("casaguide.translate")

# Récupération TOLÉRANTE (V2-70) : d'une réponse JSON TRONQUÉE, on récolte toutes les
# paires « "clé": "valeur" » COMPLÈTES (chaînes JSON, échappements inclus) et on ignore la
# dernière paire coupée — au lieu de tout jeter. La valeur incomplète en fin est perdue,
# jamais les précédentes.
_PAIR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _parse_translations(raw: str) -> dict:
    """Parse la réponse du traducteur. JSON strict d'abord ; sinon (tronqué/malformé)
    récupère les paires complètes (V2-70) — un lot tronqué livre quand même ses entrées
    valides. Ne renvoie que des valeurs chaînes."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str)}
    except ValueError:
        pass
    out: dict[str, str] = {}
    for k, v in _PAIR_RE.findall(raw):
        try:
            out[json.loads('"' + k + '"')] = json.loads('"' + v + '"')
        except ValueError:
            continue
    return out


def _chunked(items: list, size: int) -> list[list]:
    """Découpe une liste en tranches d'au plus `size` (≥ 1)."""
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]

# Seuls ces types de champ portent du texte libre à traduire. Les autres
# (time, bool, number, url, phone, select) sont structurés : jamais traduits.
TRANSLATABLE_TYPES = {"text", "textarea"}


# ── Extraction / réinjection du texte traduisible d'une section ──────────────

def collect_section_texts(schema: dict, content: dict,
                          body_md: str | None,
                          title_override: str | None = None) -> dict[str, str]:
    """Segments traduisibles d'une section, indexés par une référence stable.

    Références : `f:<key>` (champ simple), `r:<rkey>:<i>:<key>` (champ d'un item
    de groupe répétable), `body` (texte libre), `title` (titre de rubrique
    personnalisé, V2-42 — motif de `body`). Seules les chaînes non vides des
    champs `text`/`textarea` (et body/title) sont retenues."""
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
    if title_override and title_override.strip():
        out["title"] = title_override
    return out


def apply_section_texts(schema: dict, content: dict, body_md: str | None,
                        tr: dict[str, str]) -> tuple[dict, str | None, str | None]:
    """Reconstruit (content, body_md, title_override) traduits : copie de la source
    dont seuls les segments présents dans `tr` sont remplacés. Les champs structurés
    et les segments non traduits restent tels quels (repli élégant sur la source ;
    un titre non traduit → `None`, le rendu retombe sur la source, V2-42)."""
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
    ttitle = tr.get("title") if "title" in tr else None
    return tcontent, tbody, ttitle


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

        Par LOTS bornés (V2-70, `settings.translate_batch_size`) : un guide dense dépassait
        la limite de tokens en un seul appel → réponse TRONQUÉE. Chaque lot est indépendant
        (un lot qui casse n'emporte pas les autres) ; parsing tolérant + une re-tentative des
        seules clés manquantes du lot fautif. Ne renvoie que les clés effectivement traduites
        (chaînes non vides) : une clé manquante retombe sur la source au rendu (jamais de trou)."""
        if not texts:
            return {}, {"units": 0, "cost_cts": 0.0}
        result: dict[str, str] = {}
        units = 0
        cost = 0.0
        keys = list(texts)
        for chunk_keys in _chunked(keys, settings.translate_batch_size):
            chunk = {k: texts[k] for k in chunk_keys}
            got, m = self._translate_chunk(chunk, target_lang, source_lang)
            result.update(got)
            units += m["units"]
            cost += m["cost_cts"]
        result = {k: v for k, v in result.items()
                  if k in texts and isinstance(v, str) and v.strip()}
        return result, {"units": units, "cost_cts": round(cost, 4)}

    def _translate_chunk(self, chunk: dict[str, str], target_lang: str,
                         source_lang: str) -> tuple[dict[str, str], dict]:
        """Traduit UN lot. Best-effort : jamais d'exception (un appel/parse fautif renvoie
        ce qui a pu être récupéré). Re-tente UNE fois les seules clés manquantes (réponse
        tronquée : le reste, plus court, passe)."""
        try:
            got, meta = self._one_call(chunk, target_lang, source_lang)
        except Exception as exc:  # noqa: BLE001 — un lot ne fait jamais tomber la langue
            log.warning("Traduction %s : lot en échec (%s) — ignoré", target_lang, exc)
            return {}, {"units": 0, "cost_cts": 0.0}
        missing = {k: v for k, v in chunk.items() if k not in got}
        if missing and len(missing) < len(chunk):        # progrès → re-tenter le reste
            try:
                got2, m2 = self._one_call(missing, target_lang, source_lang)
                got.update(got2)
                meta = {"units": meta["units"] + m2["units"],
                        "cost_cts": meta["cost_cts"] + m2["cost_cts"]}
            except Exception as exc:  # noqa: BLE001
                log.warning("Traduction %s : re-tentative du lot en échec (%s)",
                            target_lang, exc)
        return got, meta

    def _one_call(self, texts: dict[str, str], target_lang: str,
                  source_lang: str) -> tuple[dict[str, str], dict]:
        """Un appel API : renvoie ({clé: traduction}, méta). Parsing TOLÉRANT (V2-70) :
        une réponse tronquée livre ses paires complètes plutôt que de tout jeter."""
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
        data = _parse_translations(raw)
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
        # Cibles : celles imposées par l'appelant (API, déjà plafonnées par le
        # plan) ; à défaut (chemin CLI) les langues PUBLIÉES du registre (V2-21a) —
        # jamais une liste MVP en dur (invariant 8 étendu aux langues).
        fallback = db.published_language_codes(conn)
        langs = [l for l in (target_langs or fallback)
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

            # GRANULARITÉ DE L'ÉCHEC (V2-70) : chaque langue est indépendante — une langue
            # en échec ne fait plus tout tomber (fin de l'all-or-nothing). Les langues
            # RÉUSSIES sont livrées ; une langue fautive est simplement omise de la publication.
            published: list[str] = []
            for lang in langs:
                try:
                    n = _translate_lang(conn, property_id, job_id, lang, source_lang,
                                        sections, pois, translator, summary)
                    conn.commit()
                    summary["langs"][lang] = n
                    published.append(lang)
                except Exception as exc:  # noqa: BLE001 — isole la langue fautive
                    conn.rollback()
                    summary["langs"][lang] = f"failed: {type(exc).__name__}"
                    log.warning("Traduction %s (logement %s) en échec : %s",
                                lang, property_id, exc)

            if published:
                # Publie les langues LIVRÉES (libellés fixes + name_i18n du seed localisés
                # même sans texte propriétaire ; segments non traduits → repli source).
                # Si TOUT échoue, on NE touche PAS `published_langs` (l'état antérieur, avec
                # son repli élégant, reste servi) et le job est 'failed'.
                db.set_published_langs(conn, property_id, published)
                db.job_finish(conn, job_id, "done")
            else:
                db.job_finish(conn, job_id, "failed",
                              error="aucune langue traduite")
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
                                      sec.get("body_md"), sec.get("title_override"))
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
        content2, body2, title2 = apply_section_texts(
            meta_sec["schema"], meta_sec["content"], meta_sec["body_md"],
            sec_tr[sid])
        db.upsert_section_translation(conn, sid, lang, content2, body2, title2)

    for pid in pending_pois:
        tr = poi_tr[pid]
        db.upsert_poi_translation(conn, pid, lang,
                                  tr.get("description_md"), tr.get("owner_comment"))

    n = len(pending_sections) + len(pending_pois)
    summary["translated"] += n
    return n
