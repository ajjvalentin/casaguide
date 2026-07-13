"""Rendu HTML du guide voyageur (M-08, §3.2 du CdC).

L'endpoint public `GET /g/{token}` sert une **page HTML** (et non plus le JSON
brut) : contenu rendu côté serveur pour être robuste (accessible sans JS,
consultable hors-ligne après mise en cache, testable) puis enrichi côté client
par les modules de `frontend/guide/` (carte Leaflet, filtres, visionneuse,
QR wifi, PWA).

Principes :
  * tout est déjà en base — aucun appel externe (invariant 4) ;
  * les secrets (wifi, boîte à clés) ne sont **jamais** dans ce HTML : ils sont
    chargés à la demande via `GET /g/{token}/secrets` (déchiffrement à la
    demande, §8) et injectés côté client dans les emplacements réservés ;
  * échappement systématique du contenu propriétaire (`html.escape`) : le
    Markdown est transformé après échappement — aucun HTML injectable.

L'identité visuelle (sable/mer, Fraunces + Instrument Sans, cartes « distance
d'abord », liseré de couleur de chapitre) est celle du prototype validé
`guide_preview.html`, industrialisée dans `frontend/guide/guide.css`.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

# ── Métadonnées de chapitre (nom + couleur), alignées sur frontend/js/constants.js
_CHAPTERS: dict[str, tuple[str, str]] = {
    "A": ("Arrivée & départ", "#546E7A"),
    "B": ("Le logement", "#0E5A73"),
    "C": ("Vie pratique", "#2E7D32"),
    "D": ("Urgences & santé", "#C62828"),
    "E": ("Services à la demande", "#6A1B9A"),
    "F": ("Restaurants & sorties", "#EF6C00"),
    "G": ("Activités & tourisme", "#0277BD"),
    "H": ("Transports", "#00695C"),
    "I": ("Informations", "#6D4C41"),
}
_CHAPTER_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]

# Libellés lisibles de quelques valeurs techniques de select (cf. constants.js)
_OPTION_LABELS = {
    "private": "Place privée",
    "street": "Stationnement dans la rue",
    "public": "Parking public",
}

_esc = html.escape


def _fr(i18n: Any, fallback: str = "") -> str:
    """Valeur française d'un libellé i18n (dict {fr,en,es} ou chaîne)."""
    if not i18n:
        return fallback
    if isinstance(i18n, str):
        return i18n
    if isinstance(i18n, dict):
        return i18n.get("fr") or i18n.get("en") or i18n.get("es") or fallback
    return fallback


# ── Markdown minimal et sûr (paragraphes, gras, listes) ──────────────────────

def _md_to_html(text: str | None) -> str:
    """Transforme un sous-ensemble de Markdown en HTML, **après échappement**.

    Gère : paragraphes (ligne vide), retours à la ligne simples (`<br>`), listes
    à puces (`- ` / `* `) et gras (`**texte**`). Aucun HTML brut n'est conservé
    (le texte est échappé d'abord) : rien d'injectable côté voyageur."""
    if not text:
        return ""
    safe = _esc(text.replace("\r\n", "\n").replace("\r", "\n"))
    blocks = re.split(r"\n[ \t]*\n", safe)
    out: list[str] = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"^[-*]\s+", ln) for ln in lines):
            items = "".join("<li>" + _bold(re.sub(r"^[-*]\s+", "", ln)) + "</li>"
                            for ln in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append("<p>" + "<br>".join(_bold(ln) for ln in lines) + "</p>")
    return "".join(out)


def _bold(text: str) -> str:
    """`**gras**` → `<strong>` (le texte est déjà échappé en amont)."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


# ── Distance « voyageur » : à pied si ≤ 30 min, sinon voiture (§M-01) ─────────

def _fmt_dist(poi: dict) -> tuple[str, str]:
    walk = poi.get("walk_min")
    drive = poi.get("drive_min")
    if walk is not None and walk <= 30:
        return str(walk), "min à pied"
    if drive is not None:
        return str(drive), "min en voiture"
    return "–", ""


# ── Rendu des champs d'une section (selon field_schema) ──────────────────────

def _render_fields(schema: dict, content: dict) -> str:
    rows: list[str] = []
    for f in schema.get("fields", []):
        key = f.get("key")
        val = content.get(key)
        if val is None or val == "":
            continue
        label = _esc(_fr(f.get("label"), key or ""))
        typ = f.get("type")
        if typ == "bool":
            dd = "Oui" if val else "Non"
        elif typ == "url":
            u = _esc(str(val))
            dd = f'<a href="{u}" target="_blank" rel="noopener nofollow">{u}</a>'
        elif typ == "phone":
            v = _esc(str(val))
            dd = f'<a href="tel:{_tel(str(val))}">{v}</a>'
        elif typ == "select":
            dd = _esc(_OPTION_LABELS.get(val, str(val)))
        elif typ == "textarea":
            dd = _md_to_html(str(val))
        else:
            dd = _esc(str(val))
        rows.append(f'<div class="frow"><dt>{label}</dt><dd>{dd}</dd></div>')

    # Groupes répétables (équipements, services…)
    repeat = schema.get("repeat")
    if repeat:
        arr = content.get(repeat.get("key")) or []
        cards: list[str] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            inner: list[str] = []
            for rf in repeat.get("fields", []):
                rv = item.get(rf.get("key"))
                if rv is None or rv == "":
                    continue
                inner.append(
                    f'<div class="frow"><dt>{_esc(_fr(rf.get("label"), ""))}</dt>'
                    f'<dd>{_md_to_html(str(rv)) if rf.get("type") == "textarea" else _esc(str(rv))}</dd></div>')
            if inner:
                cards.append('<div class="repeat-card"><dl>' + "".join(inner) + "</dl></div>")
        if cards:
            rows.append('<div class="repeat">' + "".join(cards) + "</div>")

    return f'<dl class="fields">{"".join(rows)}</dl>' if rows else ""


def _tel(raw: str) -> str:
    """Numéro cliquable : garde le « + » puis les chiffres."""
    raw = raw.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" if plus else "") + digits


# ── Galerie média d'une section (photos → visionneuse, PDF → lien) ───────────

def _render_media(media: list[dict]) -> str:
    if not media:
        return ""
    tiles: list[str] = []
    for m in media:
        url = _esc(m["url"])
        cap = _esc(m.get("caption") or "")
        if m.get("kind") == "photo":
            figcap = f'<figcaption>{cap}</figcaption>' if cap else ""
            tiles.append(
                f'<figure class="gphoto" data-full="{url}" data-caption="{cap}" '
                f'tabindex="0" role="button" aria-label="Agrandir la photo{" : " + cap if cap else ""}">'
                f'<img src="{url}" alt="{cap or "Photo"}" loading="lazy">{figcap}</figure>')
        else:
            label = cap or "Document PDF"
            tiles.append(
                f'<a class="gpdf" href="{url}" target="_blank" rel="noopener">'
                f'<span class="ic">PDF</span><span>{label}</span></a>')
    return f'<div class="gallery">{"".join(tiles)}</div>'


# ── Contacts (§4.D) : boutons appeler / WhatsApp / email ─────────────────────

def _render_contact(contact: dict) -> str:
    btns: list[str] = []
    phone = contact.get("phone")
    wa = contact.get("whatsapp")
    email = contact.get("email")
    if phone:
        btns.append(f'<a class="cbtn call" href="tel:{_tel(phone)}">'
                    f'<b>Appeler</b><span>{_esc(phone)}</span></a>')
    if wa:
        btns.append(f'<a class="cbtn wa" href="https://wa.me/{_tel(wa).lstrip("+")}" '
                    f'target="_blank" rel="noopener"><b>WhatsApp</b><span>{_esc(wa)}</span></a>')
    if email:
        btns.append(f'<a class="cbtn mail" href="mailto:{_esc(email)}">'
                    f'<b>Email</b><span>{_esc(email)}</span></a>')
    if not btns:
        return ""
    name = _esc(contact.get("name") or "")
    who = f'<p class="contact-who">{name}</p>' if name else ""
    return f'<div class="contact-card">{who}<div class="cbtns">{"".join(btns)}</div></div>'


# ── Section complète ─────────────────────────────────────────────────────────

def _render_section(sec: dict, contact: dict, tourism_license: str | None) -> str:
    schema = sec.get("field_schema") or {}
    content = sec.get("content") or {}
    title = _esc(_fr(sec.get("name_i18n"), sec.get("code", "")))
    parts: list[str] = [f"<h3>{title}</h3>"]

    fields_html = _render_fields(schema, content)
    if fields_html:
        parts.append(fields_html)

    body_html = _md_to_html(sec.get("body_md"))
    if body_html:
        parts.append(f'<div class="prose">{body_html}</div>')

    # Coordonnées de contact (section D_contact) et licence (section I_license)
    if schema.get("uses_property_contact"):
        parts.append(_render_contact(contact))
    if schema.get("uses_property_license") and tourism_license:
        parts.append(f'<p class="license"><span class="lic-lbl">Licence touristique</span>'
                     f'<span class="lic-val">{_esc(tourism_license)}</span></p>')

    # Emplacements réservés aux secrets (remplis côté client depuis /secrets)
    if "wifi_pass" in (schema.get("secrets") or []):
        parts.append('<div class="secret-slot" data-secret="wifi" hidden></div>')
    if "keybox_code" in (schema.get("secrets") or []):
        parts.append('<div class="secret-slot" data-secret="keybox" hidden></div>')

    parts.append(_render_media(sec.get("media") or []))
    return f'<article class="sec-card">{"".join(p for p in parts if p)}</article>'


# ── POI d'un chapitre, groupés par catégorie, triés par distance ─────────────

def _render_pois(pois: list[dict]) -> str:
    if not pois:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for p in pois:
        by_cat.setdefault(p["category_code"], []).append(p)
    blocks: list[str] = []
    for code, lst in by_cat.items():
        lst.sort(key=lambda p: (p.get("dist_walk_m") if p.get("dist_walk_m") is not None else 9e9))
        cat_name = _esc(_fr(lst[0].get("category_name"), code))
        cards: list[str] = []
        for p in lst:
            n, u = _fmt_dist(p)
            color = _esc(p.get("map_color") or "#0E5A73")
            desc = _md_to_html(p.get("description_md")) if p.get("description_md") else ""
            comment = (f'<p class="fav">❤ {_esc(p["owner_comment"])}</p>'
                       if p.get("owner_comment") else "")
            hours = (f'<div class="hours">{_esc(p["opening_hours"])}</div>'
                     if p.get("opening_hours") else "")
            meta: list[str] = []
            if p.get("phone"):
                meta.append(f'<a href="tel:{_tel(p["phone"])}">Appeler</a>')
            if p.get("website"):
                meta.append(f'<a href="{_esc(p["website"])}" target="_blank" rel="noopener nofollow">Site web</a>')
            if p.get("lat") is not None and p.get("lon") is not None:
                meta.append(f'<a href="https://www.google.com/maps/dir/?api=1&destination={p["lat"]},{p["lon"]}"'
                            f' target="_blank" rel="noopener">Itinéraire</a>')
            meta_html = f'<div class="meta">{"".join(meta)}</div>' if meta else ""
            cards.append(
                f'<div class="poi-card" style="border-left-color:{color}">'
                f'<div class="dist"><b>{_esc(n)}</b><span>{_esc(u)}</span></div>'
                f'<div class="poi-body"><h4>{_esc(p["name"])}</h4>{comment}'
                f'{f"<div class=prose>{desc}</div>" if desc else ""}{hours}{meta_html}</div></div>')
        blocks.append(f'<h4 class="cat-title">{cat_name} · {len(lst)}</h4>' + "".join(cards))
    return "".join(blocks)


# ── Barre d'urgences (numéros prioritaires, tel:) ────────────────────────────

def _render_sos(area_facts: dict) -> str:
    items = ((area_facts.get("emergency_numbers") or {}).get("items") or [])[:4]
    if not items:
        return ""
    cells: list[str] = []
    for it in items:
        num = str(it.get("number", ""))
        cells.append(f'<a class="sos-item" href="tel:{_tel(num)}">'
                     f'<span class="num">{_esc(num)}</span>'
                     f'<span class="lbl">{_esc(it.get("label", ""))}</span></a>')
    return f'<div class="sos">{"".join(cells)}</div>'


# ── Bloc « Bon à savoir sur place » (area_facts) ─────────────────────────────

def _render_area_facts(area_facts: dict, chapter_color: str) -> str:
    parts: list[str] = []
    waste = area_facts.get("waste_rules")
    if waste:
        containers = "".join(
            f'<li><b>{_esc(c.get("color_or_type", ""))}</b> — {_esc(c.get("accepts", ""))}</li>'
            for c in (waste.get("containers") or []))
        parts.append(
            f'<div class="facts"><b class="tt">Poubelles & tri</b>'
            f'<p>{_esc(waste.get("summary", ""))}</p>'
            f'{f"<ul>{containers}</ul>" if containers else ""}</div>')
    noise = area_facts.get("noise_rules")
    if noise:
        quiet = noise.get("quiet_hours")
        parts.append(
            f'<div class="facts"><b class="tt">Tranquillité du voisinage</b>'
            f'<p>{_esc(noise.get("summary", ""))}</p>'
            f'{f"<span class=quiet>🌙 {_esc(quiet)}</span>" if quiet else ""}</div>')
    emerg = area_facts.get("emergency_numbers")
    if emerg and emerg.get("items"):
        nums = "".join(f'<li><b>{_esc(str(i.get("number", "")))}</b> — {_esc(i.get("label", ""))}</li>'
                       for i in emerg["items"])
        notes = emerg.get("notes")
        parts.append(
            f'<div class="facts"><b class="tt">Tous les numéros utiles</b>'
            f'<ul>{nums}</ul>{f"<p class=fnote>{_esc(notes)}</p>" if notes else ""}</div>')
    if not parts:
        return ""
    return (f'<section class="chapter"><h2>Bon à savoir sur place</h2>'
            f'<div class="chapline" style="background:{chapter_color}"></div>'
            f'{"".join(parts)}</section>')


# ── Sélecteur de langue (FR actif ; structure prête pour M-09) ───────────────

_LANG_LABELS = {"fr": "Français", "en": "English", "es": "Español",
                "de": "Deutsch", "nl": "Nederlands"}


def _render_langs(default_lang: str, published_langs: list[str]) -> str:
    langs = [default_lang] + [l for l in (published_langs or []) if l != default_lang]
    if len(langs) <= 1:
        return ""  # une seule langue → pas de sélecteur (M-09 activera les autres)
    btns = []
    for l in langs:
        active = " on" if l == default_lang else ""
        dis = "" if l == default_lang else " disabled title=\"Bientôt disponible\""
        btns.append(f'<button class="lang{active}" data-lang="{_esc(l)}"{dis}>'
                    f'{_esc(l.upper())}</button>')
    return f'<div class="langs" aria-label="Langue">{"".join(btns)}</div>'


# ── Page complète ────────────────────────────────────────────────────────────

def render_guide(prop: dict, sections: list[dict], pois: list[dict],
                 area_facts: dict, token: str) -> str:
    contact = prop.get("contact") or {}
    name = _esc(prop.get("name") or "Votre logement")
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)

    # Chapitres présents (sections visibles ou POI retenus)
    present = {s["chapter"] for s in sections} | {p["chapter"] for p in pois}

    # Données embarquées pour la carte (aucun second appel réseau)
    map_data = {
        "property": {"name": prop.get("name"), "lat": prop.get("lat"), "lon": prop.get("lon")},
        "pois": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                  "chapter": p["chapter"], "color": p.get("map_color"),
                  "category": _fr(p.get("category_name"), p["category_code"]),
                  "walk_min": p.get("walk_min"), "drive_min": p.get("drive_min"),
                  "phone": p.get("phone")}
                 for p in pois if p.get("lat") is not None and p.get("lon") is not None],
    }
    data_json = json.dumps(map_data, ensure_ascii=False).replace("</", "<\\/")

    # Filtres par chapitre (puces)
    chips = ['<button class="chip on" data-chapter="">Tout</button>']
    for ch in _CHAPTER_ORDER:
        if ch in present:
            chips.append(f'<button class="chip" data-chapter="{ch}">{_esc(_CHAPTERS[ch][0])}</button>')

    # Corps : un bloc par chapitre (sections visibles + POI du chapitre)
    body: list[str] = []
    for ch in _CHAPTER_ORDER:
        if ch not in present:
            continue
        ch_name, ch_color = _CHAPTERS[ch]
        inner: list[str] = []
        for sec in [s for s in sections if s["chapter"] == ch]:
            inner.append(_render_section(sec, contact, prop.get("tourism_license")))
        inner.append(_render_pois([p for p in pois if p["chapter"] == ch]))
        body.append(
            f'<section class="chapter" data-chapter="{ch}">'
            f'<h2>{_esc(ch_name)}</h2>'
            f'<div class="chapline" style="background:{ch_color}"></div>'
            f'{"".join(x for x in inner if x)}</section>')

    body.append(_render_area_facts(area_facts, _CHAPTERS["I"][1]))

    sos = _render_sos(area_facts)
    langs = _render_langs(prop.get("default_lang") or "fr", prop.get("published_langs") or [])
    has_map = map_data["property"]["lat"] is not None

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#0E5A73">
<title>{name} — Guide du logement</title>
<link rel="manifest" href="/g/{_esc(token)}/manifest.webmanifest">
<link rel="apple-touch-icon" href="/guide/icon-192.png">
<link rel="icon" href="/guide/icon-192.png">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/guide/guide.css">
</head>
<body data-token="{_esc(token)}">
<div class="wrap">
  <header class="guide-head">
    <div class="hrow">
      <div>
        <div class="eyebrow">Votre guide de séjour</div>
        <h1>{name}</h1>
        {f'<div class="city">{_esc(place)}</div>' if place else ''}
      </div>
      {langs}
    </div>
    {sos}
  </header>
  {'<div id="map"></div>' if has_map else ''}
  <nav class="chips" aria-label="Filtrer par thème">{"".join(chips)}</nav>
  <main id="content">{"".join(body)}</main>
  <footer>Guide propulsé par CasaGuide — données OpenStreetMap. Bon séjour !</footer>
</div>
<script id="guide-data" type="application/json">{data_json}</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script type="module" src="/guide/app.js"></script>
</body>
</html>"""


def build_manifest(prop: dict, token: str) -> dict:
    """Manifest PWA propre au guide : `start_url`/`scope` pointent sur ce guide
    précis (multi-tenant), le nom reprend celui du logement."""
    base = f"/g/{token}"
    return {
        "name": f"{prop.get('name') or 'CasaGuide'} — Guide du logement",
        "short_name": (prop.get("name") or "CasaGuide")[:24],
        "description": "Votre guide d'accueil : arrivée, wifi, urgences, "
                       "commerces, restaurants et carte du quartier.",
        "lang": prop.get("default_lang") or "fr",
        "start_url": base,
        "scope": base,
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#FAF7F2",
        "theme_color": "#0E5A73",
        "icons": [
            {"src": "/guide/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/guide/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/guide/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


def render_not_found() -> str:
    """Page 404 propre : token inconnu ou logement non publié (on ne révèle rien)."""
    return """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Guide introuvable</title>
<link rel="stylesheet" href="/guide/guide.css">
</head>
<body>
<div class="wrap notfound">
  <div class="nf-card">
    <div class="nf-emoji">🧭</div>
    <h1>Guide introuvable</h1>
    <p>Ce lien n'est pas (ou plus) actif. Vérifiez l'adresse auprès de votre hôte,
       ou demandez-lui un nouveau lien.</p>
  </div>
</div>
</body>
</html>"""
