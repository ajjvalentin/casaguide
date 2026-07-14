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

# ── Couleurs de chapitre (alignées sur frontend/js/constants.js) ─────────────
_CHAPTER_COLORS: dict[str, str] = {
    "A": "#546E7A", "B": "#0E5A73", "C": "#2E7D32", "D": "#C62828",
    "E": "#6A1B9A", "F": "#EF6C00", "G": "#0277BD", "H": "#00695C", "I": "#6D4C41",
}
_CHAPTER_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]

# Noms de chapitre localisés (M-09). Le français reste la source/repli.
_CHAPTER_NAMES: dict[str, dict[str, str]] = {
    "fr": {"A": "Arrivée & départ", "B": "Le logement", "C": "Vie pratique",
           "D": "Urgences & santé", "E": "Services à la demande",
           "F": "Restaurants & sorties", "G": "Activités & tourisme",
           "H": "Transports", "I": "Informations"},
    "en": {"A": "Arrival & departure", "B": "The home", "C": "Everyday life",
           "D": "Emergencies & health", "E": "On-demand services",
           "F": "Dining & going out", "G": "Activities & sightseeing",
           "H": "Getting around", "I": "Information"},
    "es": {"A": "Llegada y salida", "B": "El alojamiento", "C": "Vida práctica",
           "D": "Urgencias y salud", "E": "Servicios a demanda",
           "F": "Restaurantes y salidas", "G": "Actividades y turismo",
           "H": "Transporte", "I": "Información"},
}

# Libellés lisibles de quelques valeurs techniques de select (cf. constants.js),
# localisés (M-09).
_OPTION_LABELS: dict[str, dict[str, str]] = {
    "private": {"fr": "Place privée", "en": "Private space", "es": "Plaza privada"},
    "street": {"fr": "Stationnement dans la rue", "en": "Street parking",
               "es": "Aparcamiento en la calle"},
    "public": {"fr": "Parking public", "en": "Public car park",
               "es": "Aparcamiento público"},
}

# Dictionnaire statique des libellés fixes de l'interface du guide (M-09, §9).
# Toute clé absente d'une langue retombe sur le français.
_UI: dict[str, dict[str, str]] = {
    "fr": {
        "eyebrow": "Votre guide de séjour", "all": "Tout",
        "walk": "min à pied", "drive": "min en voiture",
        "call": "Appeler", "email": "Email", "route": "Itinéraire",
        "website": "Site web", "yes": "Oui", "no": "Non",
        "license": "Licence touristique", "pdf": "Document PDF",
        "photo": "Photo", "enlarge": "Agrandir la photo",
        "good_to_know": "Bon à savoir sur place", "waste": "Poubelles & tri",
        "noise": "Tranquillité du voisinage", "numbers": "Tous les numéros utiles",
        "filter": "Filtrer par thème", "lang": "Langue",
        "title_suffix": "Guide du logement", "home": "Votre logement",
        "footer": "Guide propulsé par CasaGuide — données OpenStreetMap. Bon séjour !",
    },
    "en": {
        "eyebrow": "Your stay guide", "all": "All",
        "walk": "min walk", "drive": "min by car",
        "call": "Call", "email": "Email", "route": "Directions",
        "website": "Website", "yes": "Yes", "no": "No",
        "license": "Tourist licence", "pdf": "PDF document",
        "photo": "Photo", "enlarge": "Enlarge photo",
        "good_to_know": "Good to know", "waste": "Waste & recycling",
        "noise": "Neighbourhood quiet", "numbers": "All useful numbers",
        "filter": "Filter by theme", "lang": "Language",
        "title_suffix": "Property guide", "home": "Your accommodation",
        "footer": "Guide powered by CasaGuide — OpenStreetMap data. Enjoy your stay!",
    },
    "es": {
        "eyebrow": "Tu guía de estancia", "all": "Todo",
        "walk": "min a pie", "drive": "min en coche",
        "call": "Llamar", "email": "Correo", "route": "Cómo llegar",
        "website": "Sitio web", "yes": "Sí", "no": "No",
        "license": "Licencia turística", "pdf": "Documento PDF",
        "photo": "Foto", "enlarge": "Ampliar la foto",
        "good_to_know": "Bueno saber en el lugar", "waste": "Basura y reciclaje",
        "noise": "Tranquilidad del vecindario", "numbers": "Todos los números útiles",
        "filter": "Filtrar por tema", "lang": "Idioma",
        "title_suffix": "Guía del alojamiento", "home": "Tu alojamiento",
        "footer": "Guía con tecnología de CasaGuide — datos de OpenStreetMap. ¡Feliz estancia!",
    },
}

# Noms lisibles des langues (natifs) pour le sélecteur.
_LANG_LABELS = {"fr": "Français", "en": "English", "es": "Español",
                "de": "Deutsch", "nl": "Nederlands"}

_esc = html.escape


def _t(lang: str, key: str) -> str:
    """Libellé fixe de l'interface dans `lang` (repli français)."""
    return _UI.get(lang, {}).get(key) or _UI["fr"][key]


def _i18n(i18n: Any, lang: str = "fr", fallback: str = "") -> str:
    """Valeur localisée d'un libellé i18n (dict {fr,en,es…} ou chaîne).
    Repli : `lang` → fr → en → es → `fallback` (jamais de trou, §9)."""
    if not i18n:
        return fallback
    if isinstance(i18n, str):
        return i18n
    if isinstance(i18n, dict):
        return (i18n.get(lang) or i18n.get("fr") or i18n.get("en")
                or i18n.get("es") or fallback)
    return fallback


def _fr(i18n: Any, fallback: str = "") -> str:
    """Raccourci « langue française » (cahier staff M-13, resté FR)."""
    return _i18n(i18n, "fr", fallback)


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

def _fmt_dist(poi: dict, lang: str = "fr") -> tuple[str, str]:
    walk = poi.get("walk_min")
    drive = poi.get("drive_min")
    if walk is not None and walk <= 30:
        return str(walk), _t(lang, "walk")
    if drive is not None:
        return str(drive), _t(lang, "drive")
    return "–", ""


# ── Rendu des champs d'une section (selon field_schema) ──────────────────────

def _render_fields(schema: dict, content: dict, lang: str = "fr") -> str:
    rows: list[str] = []
    for f in schema.get("fields", []):
        key = f.get("key")
        val = content.get(key)
        if val is None or val == "":
            continue
        label = _esc(_i18n(f.get("label"), lang, key or ""))
        typ = f.get("type")
        if typ == "bool":
            dd = _t(lang, "yes") if val else _t(lang, "no")
        elif typ == "url":
            u = _esc(str(val))
            dd = f'<a href="{u}" target="_blank" rel="noopener nofollow">{u}</a>'
        elif typ == "phone":
            v = _esc(str(val))
            dd = f'<a href="tel:{_tel(str(val))}">{v}</a>'
        elif typ == "select":
            dd = _esc(_i18n(_OPTION_LABELS.get(val), lang, str(val)))
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
                    f'<div class="frow"><dt>{_esc(_i18n(rf.get("label"), lang, ""))}</dt>'
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

def _render_media(media: list[dict], lang: str = "fr") -> str:
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
                f'tabindex="0" role="button" aria-label="{_t(lang, "enlarge")}{" : " + cap if cap else ""}">'
                f'<img src="{url}" alt="{cap or _t(lang, "photo")}" loading="lazy">{figcap}</figure>')
        else:
            label = cap or _t(lang, "pdf")
            tiles.append(
                f'<a class="gpdf" href="{url}" target="_blank" rel="noopener">'
                f'<span class="ic">PDF</span><span>{label}</span></a>')
    return f'<div class="gallery">{"".join(tiles)}</div>'


# ── Contacts (§4.D) : boutons appeler / WhatsApp / email ─────────────────────

def _render_contact(contact: dict, lang: str = "fr") -> str:
    btns: list[str] = []
    phone = contact.get("phone")
    wa = contact.get("whatsapp")
    email = contact.get("email")
    if phone:
        btns.append(f'<a class="cbtn call" href="tel:{_tel(phone)}">'
                    f'<b>{_t(lang, "call")}</b><span>{_esc(phone)}</span></a>')
    if wa:
        btns.append(f'<a class="cbtn wa" href="https://wa.me/{_tel(wa).lstrip("+")}" '
                    f'target="_blank" rel="noopener"><b>WhatsApp</b><span>{_esc(wa)}</span></a>')
    if email:
        btns.append(f'<a class="cbtn mail" href="mailto:{_esc(email)}">'
                    f'<b>{_t(lang, "email")}</b><span>{_esc(email)}</span></a>')
    if not btns:
        return ""
    name = _esc(contact.get("name") or "")
    who = f'<p class="contact-who">{name}</p>' if name else ""
    return f'<div class="contact-card">{who}<div class="cbtns">{"".join(btns)}</div></div>'


# ── Section complète ─────────────────────────────────────────────────────────

def _render_section(sec: dict, contact: dict, tourism_license: str | None,
                    lang: str = "fr") -> str:
    schema = sec.get("field_schema") or {}
    content = sec.get("content") or {}
    title = _esc(_i18n(sec.get("name_i18n"), lang, sec.get("code", "")))
    parts: list[str] = [f"<h3>{title}</h3>"]

    fields_html = _render_fields(schema, content, lang)
    if fields_html:
        parts.append(fields_html)

    body_html = _md_to_html(sec.get("body_md"))
    if body_html:
        parts.append(f'<div class="prose">{body_html}</div>')

    # Coordonnées de contact (section D_contact) et licence (section I_license)
    if schema.get("uses_property_contact"):
        parts.append(_render_contact(contact, lang))
    if schema.get("uses_property_license") and tourism_license:
        parts.append(f'<p class="license"><span class="lic-lbl">{_t(lang, "license")}</span>'
                     f'<span class="lic-val">{_esc(tourism_license)}</span></p>')

    # Emplacements réservés aux secrets (remplis côté client depuis /secrets)
    if "wifi_pass" in (schema.get("secrets") or []):
        parts.append('<div class="secret-slot" data-secret="wifi" hidden></div>')
    if "keybox_code" in (schema.get("secrets") or []):
        parts.append('<div class="secret-slot" data-secret="keybox" hidden></div>')

    parts.append(_render_media(sec.get("media") or [], lang))
    return f'<article class="sec-card">{"".join(p for p in parts if p)}</article>'


# ── POI d'un chapitre, groupés par catégorie, triés par distance ─────────────

def _render_pois(pois: list[dict], lang: str = "fr") -> str:
    if not pois:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for p in pois:
        by_cat.setdefault(p["category_code"], []).append(p)
    blocks: list[str] = []
    for code, lst in by_cat.items():
        lst.sort(key=lambda p: (p.get("dist_walk_m") if p.get("dist_walk_m") is not None else 9e9))
        cat_name = _esc(_i18n(lst[0].get("category_name"), lang, code))
        cards: list[str] = []
        for p in lst:
            n, u = _fmt_dist(p, lang)
            color = _esc(p.get("map_color") or "#0E5A73")
            desc = _md_to_html(p.get("description_md")) if p.get("description_md") else ""
            comment = (f'<p class="fav">❤ {_esc(p["owner_comment"])}</p>'
                       if p.get("owner_comment") else "")
            hours = (f'<div class="hours">{_esc(p["opening_hours"])}</div>'
                     if p.get("opening_hours") else "")
            meta: list[str] = []
            if p.get("phone"):
                meta.append(f'<a href="tel:{_tel(p["phone"])}">{_t(lang, "call")}</a>')
            if p.get("website"):
                meta.append(f'<a href="{_esc(p["website"])}" target="_blank" rel="noopener nofollow">{_t(lang, "website")}</a>')
            if p.get("lat") is not None and p.get("lon") is not None:
                meta.append(f'<a href="https://www.google.com/maps/dir/?api=1&destination={p["lat"]},{p["lon"]}"'
                            f' target="_blank" rel="noopener">{_t(lang, "route")}</a>')
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

def _render_area_facts(area_facts: dict, chapter_color: str, lang: str = "fr") -> str:
    # NB : le contenu d'area_facts est généré en français par le pipeline ; seuls
    # les intitulés de rubrique sont localisés (traduction du contenu : V2).
    parts: list[str] = []
    waste = area_facts.get("waste_rules")
    if waste:
        containers = "".join(
            f'<li><b>{_esc(c.get("color_or_type", ""))}</b> — {_esc(c.get("accepts", ""))}</li>'
            for c in (waste.get("containers") or []))
        parts.append(
            f'<div class="facts"><b class="tt">{_t(lang, "waste")}</b>'
            f'<p>{_esc(waste.get("summary", ""))}</p>'
            f'{f"<ul>{containers}</ul>" if containers else ""}</div>')
    noise = area_facts.get("noise_rules")
    if noise:
        quiet = noise.get("quiet_hours")
        parts.append(
            f'<div class="facts"><b class="tt">{_t(lang, "noise")}</b>'
            f'<p>{_esc(noise.get("summary", ""))}</p>'
            f'{f"<span class=quiet>🌙 {_esc(quiet)}</span>" if quiet else ""}</div>')
    emerg = area_facts.get("emergency_numbers")
    if emerg and emerg.get("items"):
        nums = "".join(f'<li><b>{_esc(str(i.get("number", "")))}</b> — {_esc(i.get("label", ""))}</li>'
                       for i in emerg["items"])
        notes = emerg.get("notes")
        parts.append(
            f'<div class="facts"><b class="tt">{_t(lang, "numbers")}</b>'
            f'<ul>{nums}</ul>{f"<p class=fnote>{_esc(notes)}</p>" if notes else ""}</div>')
    if not parts:
        return ""
    return (f'<section class="chapter"><h2>{_t(lang, "good_to_know")}</h2>'
            f'<div class="chapline" style="background:{chapter_color}"></div>'
            f'{"".join(parts)}</section>')


# ── Sélecteur de langue (M-09) : liens ?lang=xx, rendu côté serveur ──────────

def _render_langs(default_lang: str, published_langs: list[str],
                  current_lang: str) -> str:
    """Sélecteur de langue : la langue source + les langues publiées. Chaque
    entrée est un lien `?lang=xx` (rendu serveur) ; l'app peut mémoriser le choix
    (localStorage) et détecter `navigator.language` au premier chargement."""
    langs = [default_lang] + [l for l in (published_langs or []) if l != default_lang]
    if len(langs) <= 1:
        return ""  # une seule langue → pas de sélecteur
    btns = []
    for l in langs:
        active = " on" if l == current_lang else ""
        aria = ' aria-current="true"' if l == current_lang else ""
        href = "?lang=" + _esc(l) if l != default_lang else "?lang=" + _esc(default_lang)
        btns.append(f'<a class="lang{active}" href="{href}" data-lang="{_esc(l)}"{aria} '
                    f'title="{_esc(_LANG_LABELS.get(l, l.upper()))}">{_esc(l.upper())}</a>')
    return f'<div class="langs" aria-label="{_t(current_lang, "lang")}">{"".join(btns)}</div>'


# ── Page complète ────────────────────────────────────────────────────────────

def _chapter_name(ch: str, lang: str) -> str:
    """Nom localisé d'un chapitre (repli français)."""
    return (_CHAPTER_NAMES.get(lang, {}).get(ch)
            or _CHAPTER_NAMES["fr"].get(ch, ch))


def render_guide(prop: dict, sections: list[dict], pois: list[dict],
                 area_facts: dict, token: str, lang: str = "fr") -> str:
    contact = prop.get("contact") or {}
    name = _esc(prop.get("name") or _t(lang, "home"))
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)

    # Chapitres présents (sections visibles ou POI retenus)
    present = {s["chapter"] for s in sections} | {p["chapter"] for p in pois}

    # Données embarquées pour la carte (aucun second appel réseau)
    map_data = {
        "property": {"name": prop.get("name"), "lat": prop.get("lat"), "lon": prop.get("lon")},
        "pois": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                  "chapter": p["chapter"], "color": p.get("map_color"),
                  "category": _i18n(p.get("category_name"), lang, p["category_code"]),
                  "walk_min": p.get("walk_min"), "drive_min": p.get("drive_min"),
                  "phone": p.get("phone")}
                 for p in pois if p.get("lat") is not None and p.get("lon") is not None],
    }
    data_json = json.dumps(map_data, ensure_ascii=False).replace("</", "<\\/")

    # Filtres par chapitre (puces)
    chips = [f'<button class="chip on" data-chapter="">{_esc(_t(lang, "all"))}</button>']
    for ch in _CHAPTER_ORDER:
        if ch in present:
            chips.append(f'<button class="chip" data-chapter="{ch}">{_esc(_chapter_name(ch, lang))}</button>')

    # Corps : un bloc par chapitre (sections visibles + POI du chapitre)
    body: list[str] = []
    for ch in _CHAPTER_ORDER:
        if ch not in present:
            continue
        ch_color = _CHAPTER_COLORS[ch]
        inner: list[str] = []
        for sec in [s for s in sections if s["chapter"] == ch]:
            inner.append(_render_section(sec, contact, prop.get("tourism_license"), lang))
        inner.append(_render_pois([p for p in pois if p["chapter"] == ch], lang))
        body.append(
            f'<section class="chapter" data-chapter="{ch}">'
            f'<h2>{_esc(_chapter_name(ch, lang))}</h2>'
            f'<div class="chapline" style="background:{ch_color}"></div>'
            f'{"".join(x for x in inner if x)}</section>')

    body.append(_render_area_facts(area_facts, _CHAPTER_COLORS["I"], lang))

    sos = _render_sos(area_facts)
    default_lang = prop.get("default_lang") or "fr"
    langs = _render_langs(default_lang, prop.get("published_langs") or [], lang)
    has_map = map_data["property"]["lat"] is not None

    return f"""<!DOCTYPE html>
<html lang="{_esc(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#0E5A73">
<title>{name} — {_esc(_t(lang, "title_suffix"))}</title>
<link rel="manifest" href="/g/{_esc(token)}/manifest.webmanifest">
<link rel="apple-touch-icon" href="/guide/icon-192.png">
<link rel="icon" href="/guide/icon-192.png">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/guide/guide.css">
</head>
<body data-token="{_esc(token)}" data-lang="{_esc(lang)}" data-default-lang="{_esc(default_lang)}">
<div class="wrap">
  <header class="guide-head">
    <div class="hrow">
      <div>
        <div class="eyebrow">{_esc(_t(lang, "eyebrow"))}</div>
        <h1>{name}</h1>
        {f'<div class="city">{_esc(place)}</div>' if place else ''}
      </div>
      {langs}
    </div>
    {sos}
  </header>
  {'<div id="map"></div>' if has_map else ''}
  <nav class="chips" aria-label="{_esc(_t(lang, "filter"))}">{"".join(chips)}</nav>
  <main id="content">{"".join(body)}</main>
  <footer>{_esc(_t(lang, "footer"))}</footer>
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


# ── Cahier de préparation « équipe d'entretien » (/s/{staff_token}, M-13) ─────
# Variante sobre du moteur de rendu M-08 : réutilise `_render_fields`,
# `_md_to_html`, `_render_media` mais SANS carte, SANS POI, SANS secrets, SANS
# area_facts (invariant 7). Mise en page « check-list » mobile. La page est
# servie même quand le logement est en brouillon (l'équipe prépare avant
# publication) : voir routers/guide.py.

def _render_staff_section(sec: dict) -> str:
    """Une section 'staff' rendue en fiche sobre (mêmes briques que le guide)."""
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
    parts.append(_render_media(sec.get("media") or []))
    body = "".join(p for p in parts if p)
    return f'<article class="sec-card staff-card">{body}</article>'


def render_staff(prop: dict, sections: list[dict], token: str) -> str:
    """Cahier de préparation mobile de l'équipe d'entretien (§M-13).

    Jamais indexé, jamais de secrets ni de POI. Reste lisible sans JS (rendu
    côté serveur). Affiche un état vide explicite si aucune consigne n'est
    encore saisie (le cahier peut être ouvert dès la création du logement)."""
    name = _esc(prop.get("name") or "Votre logement")
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)
    draft = prop.get("status") != "published"

    if sections:
        body = "".join(_render_staff_section(s) for s in sections)
    else:
        body = ('<div class="staff-empty"><p>Aucune consigne de préparation '
                "n'a encore été saisie pour ce logement. Revenez après que le "
                "propriétaire l'aura complété.</p></div>")

    draft_note = ('<div class="staff-draft">Logement en préparation — ce cahier '
                  'peut être consulté avant la mise en ligne du guide voyageur.</div>'
                  if draft else '')

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#334049">
<title>{name} — Préparation du logement</title>
<link rel="icon" href="/guide/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/guide/guide.css">
</head>
<body class="staff-page">
<div class="wrap">
  <header class="staff-head">
    <div class="eyebrow">Cahier de préparation · Équipe d'entretien</div>
    <h1>{name}</h1>
    {f'<div class="city">{_esc(place)}</div>' if place else ''}
    {draft_note}
  </header>
  <main id="content">{body}</main>
  <footer>Cahier interne CasaGuide — réservé à l'équipe de préparation.</footer>
</div>
</body>
</html>"""


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
