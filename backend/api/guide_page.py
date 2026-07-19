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
import unicodedata
from typing import Any

from .assets import versioned

# Locale Open Graph par langue (M-25) : repli fr_FR.
_OG_LOCALE = {"fr": "fr_FR", "en": "en_GB", "es": "es_ES"}

# ── Couleurs de chapitre (alignées sur frontend/js/constants.js) ─────────────
_CHAPTER_COLORS: dict[str, str] = {
    "A": "#546E7A", "B": "#0E5A73", "C": "#2E7D32", "D": "#C62828",
    "E": "#6A1B9A", "F": "#EF6C00", "G": "#0277BD", "H": "#00695C", "I": "#6D4C41",
}
_CHAPTER_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]

# Trois espaces à onglets (V2-09) : les retours testeurs unanimes (« trop
# d'informations ») imposent de casser le rouleau unique. On répartit le contenu
# SANS rien retirer :
#   · home      « Le logement »   : arrivée, maison, vie pratique, infos (+ wifi/secrets)
#   · emergency « Urgences »      : barre SOS en grand, santé (chap. D), numéros utiles
#   · around    « Autour de vous » : carte + tous les autres lieux (E/F/G/H + commerces C)
# Un chapitre voit ses SECTIONS et ses POI potentiellement dans des espaces
# différents (seul le chapitre C : sections → home, commerces → around).
# Listes de lieux repliées (V2-09) : nombre de cartes visibles par catégorie
# avant le bouton « Voir les N autres ».
_POI_VISIBLE = 4

_TAB_ORDER = ["home", "emergency", "around"]
# Ancres d'URL FIXES (non localisées) : liens profonds + retour arrière stables.
_TAB_HASH = {"home": "logement", "emergency": "urgences", "around": "autour"}
_SECTION_TAB = {"A": "home", "B": "home", "C": "home", "D": "emergency",
                "E": "around", "F": "around", "G": "around", "H": "around",
                "I": "home"}
_POI_TAB = {"A": "home", "B": "home", "C": "around", "D": "emergency",
            "E": "around", "F": "around", "G": "around", "H": "around",
            "I": "home"}

# Dérogations PAR SECTION (retour terrain 18/07) : les sections « coquilles »
# commerçantes du chapitre C (leur contenu réel = des lieux) suivent leurs POI
# dans « Autour de vous » — sans quoi elles restent orphelines et vides dans
# « Le logement ». Seule C_trash (poubelles & tri) est un vrai contenu maison.
_SECTION_TAB_OVERRIDES = {
    "C_supermarkets": "around", "C_markets": "around", "C_shops": "around",
    "C_malls": "around", "C_laundry": "around",
}

# Titres de chapitre CONTEXTUALISÉS par onglet (retour terrain 19/07) : le
# chapitre C étant écartelé entre deux onglets, son nom générique « Vie
# pratique » apparaîtrait DEUX fois pour des contenus différents. Côté
# logement il ne coiffe que le tri des déchets ; côté « Autour de vous », des
# commerces. Clé : (chapitre, onglet) → libellés par langue.
_CHAPTER_TAB_NAMES: dict[tuple[str, str], dict[str, str]] = {
    ("C", "home"): {"fr": "Vie pratique — déchets & tri",
                    "en": "Everyday life — waste & recycling",
                    "es": "Vida práctica — basura y reciclaje"},
    ("C", "around"): {"fr": "Commerces & services",
                      "en": "Shops & services",
                      "es": "Comercios y servicios"},
}

# Catégories « point de départ du trajet » : rendues comme blocs d'itinéraire
# « en un tap » dans la section qui les déclare (A_arrival), et non en cartes
# POI ordinaires (M-14). La gare routière (bus_station, M-21) rejoint les
# aéroports/gares — hub d'arrivée fréquent là où il n'y a pas de gare ferroviaire.
_TRANSPORT_CATEGORIES = {"airport", "train_station", "bus_station"}

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
        "cuisine_filter": "Filtrer par cuisine",
        "tabs": "Espaces du guide", "tab_home": "Le logement",
        "tab_emergency": "Urgences", "tab_around": "Autour de vous",
        "show_more": "Voir les {n} autres", "show_less": "Réduire",
        "nav_to_home": "Itinéraire vers le logement", "open_in": "Ouvrir dans",
        "nav_take_me": "Me guider vers le logement", "view_route": "Voir l'itinéraire",
        "address": "Adresse", "gps": "Coordonnées GPS",
        "copy": "Copier", "copied": "Copié ✓",
        "title_suffix": "Guide du logement", "home": "Votre logement",
        "share_desc": "Tout pour votre séjour : arrivée, wifi, urgences, commerces, "
                      "restaurants et carte du quartier.",
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
        "cuisine_filter": "Filter by cuisine",
        "tabs": "Guide sections", "tab_home": "The home",
        "tab_emergency": "Emergencies", "tab_around": "Around you",
        "show_more": "Show {n} more", "show_less": "Show less",
        "nav_to_home": "Directions to the property", "open_in": "Open in",
        "nav_take_me": "Take me to the property", "view_route": "View route",
        "address": "Address", "gps": "GPS coordinates",
        "copy": "Copy", "copied": "Copied ✓",
        "title_suffix": "Property guide", "home": "Your accommodation",
        "share_desc": "Everything for your stay: check-in, wifi, emergencies, shops, "
                      "restaurants and a map of the area.",
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
        "cuisine_filter": "Filtrar por cocina",
        "tabs": "Espacios de la guía", "tab_home": "El alojamiento",
        "tab_emergency": "Emergencias", "tab_around": "A tu alrededor",
        "show_more": "Ver {n} más", "show_less": "Reducir",
        "nav_to_home": "Cómo llegar al alojamiento", "open_in": "Abrir en",
        "nav_take_me": "Llévame al alojamiento", "view_route": "Ver ruta",
        "address": "Dirección", "gps": "Coordenadas GPS",
        "copy": "Copiar", "copied": "Copiado ✓",
        "title_suffix": "Guía del alojamiento", "home": "Tu alojamiento",
        "share_desc": "Todo para tu estancia: llegada, wifi, urgencias, comercios, "
                      "restaurantes y mapa del barrio.",
        "footer": "Guía con tecnología de CasaGuide — datos de OpenStreetMap. ¡Feliz estancia!",
    },
}

# Noms lisibles des langues (natifs) pour le sélecteur.
_LANG_LABELS = {"fr": "Français", "en": "English", "es": "Español",
                "de": "Deutsch", "nl": "Nederlands"}

# Libellés localisés des types de cuisine courants (M-16). Clés = valeurs OSM
# normalisées (`overpass._norm_cuisine`). Toute valeur absente retombe sur la
# valeur brute (embellie). N'a pas vocation à être exhaustif : on couvre les
# cuisines les plus fréquentes en zone touristique.
_CUISINE_LABELS: dict[str, dict[str, str]] = {
    "italian": {"fr": "Italien", "en": "Italian", "es": "Italiano"},
    "pizza": {"fr": "Pizza", "en": "Pizza", "es": "Pizza"},
    "spanish": {"fr": "Espagnol", "en": "Spanish", "es": "Español"},
    "tapas": {"fr": "Tapas", "en": "Tapas", "es": "Tapas"},
    "seafood": {"fr": "Fruits de mer", "en": "Seafood", "es": "Marisco"},
    "fish": {"fr": "Poisson", "en": "Fish", "es": "Pescado"},
    "mediterranean": {"fr": "Méditerranéen", "en": "Mediterranean", "es": "Mediterráneo"},
    "french": {"fr": "Français", "en": "French", "es": "Francés"},
    "asian": {"fr": "Asiatique", "en": "Asian", "es": "Asiático"},
    "chinese": {"fr": "Chinois", "en": "Chinese", "es": "Chino"},
    "japanese": {"fr": "Japonais", "en": "Japanese", "es": "Japonés"},
    "sushi": {"fr": "Sushi", "en": "Sushi", "es": "Sushi"},
    "thai": {"fr": "Thaïlandais", "en": "Thai", "es": "Tailandés"},
    "indian": {"fr": "Indien", "en": "Indian", "es": "Indio"},
    "mexican": {"fr": "Mexicain", "en": "Mexican", "es": "Mexicano"},
    "american": {"fr": "Américain", "en": "American", "es": "Americano"},
    "burger": {"fr": "Burger", "en": "Burger", "es": "Hamburguesa"},
    "kebab": {"fr": "Kebab", "en": "Kebab", "es": "Kebab"},
    "greek": {"fr": "Grec", "en": "Greek", "es": "Griego"},
    "vegetarian": {"fr": "Végétarien", "en": "Vegetarian", "es": "Vegetariano"},
    "vegan": {"fr": "Végan", "en": "Vegan", "es": "Vegano"},
    "steak_house": {"fr": "Grillades", "en": "Steakhouse", "es": "Carnes"},
    "barbecue": {"fr": "Grillades", "en": "Barbecue", "es": "Barbacoa"},
    "chicken": {"fr": "Poulet", "en": "Chicken", "es": "Pollo"},
    "ice_cream": {"fr": "Glaces", "en": "Ice cream", "es": "Helados"},
    "coffee_shop": {"fr": "Café", "en": "Coffee shop", "es": "Cafetería"},
    "cafe": {"fr": "Café", "en": "Café", "es": "Cafetería"},
    "sandwich": {"fr": "Sandwichs", "en": "Sandwich", "es": "Bocadillos"},
    "breakfast": {"fr": "Petit-déjeuner", "en": "Breakfast", "es": "Desayuno"},
    "international": {"fr": "International", "en": "International", "es": "Internacional"},
    "regional": {"fr": "Régional", "en": "Regional", "es": "Regional"},
}

_esc = html.escape


def _cuisine_label(value: str, lang: str = "fr") -> str:
    """Libellé localisé d'un type de cuisine (M-16), repli sur la valeur brute
    embellie (underscores → espaces, capitalisée)."""
    d = _CUISINE_LABELS.get(value)
    if d:
        return d.get(lang) or d.get("fr") or value
    return value.replace("_", " ").strip().capitalize()


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


# ── Navigation « en un tap » (M-14/M-20) : aéroport / gare → logement ────────
# Rendus DANS la section qui déclare ces catégories (A_arrival). En tête, un
# bandeau de navigation universelle (M-20) « Me guider vers le logement » — deux
# gros boutons Google Maps + Waze en destination seule : l'app part de la
# position réelle du voyageur (à l'aéroport comme ailleurs). En dessous, un bloc
# par lieu prédéfini reste un itinéraire de PLANIFICATION (durée en voiture + un
# bouton Google Maps origine→logement). Waze ne supporte pas d'origine → retiré
# des blocs (redondant avec le bandeau). Zéro saisie : tout dérive de la
# géométrie du logement et des POI. Aucun appel externe au rendu (invariant 4).

def _latlon(lat: Any, lon: Any) -> str:
    """Couple « lat,lon » pour une URL de navigation (jamais de virgule décimale)."""
    return f"{lat},{lon}"


def _render_nav_banner(home_lat: Any, home_lon: Any, lang: str = "fr") -> str:
    """Bandeau de navigation universelle (M-20) : « Me guider vers le logement ».
    Deux gros boutons en destination seule (Google Maps + Waze) — l'app démarre de
    la position réelle du voyageur. C'est LE geste principal de la section."""
    if home_lat is None or home_lon is None:
        return ""
    home = _latlon(home_lat, home_lon)
    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={home}"
    waze = f"https://waze.com/ul?ll={home}&navigate=yes"
    return (f'<div class="nav-banner" aria-label="{_esc(_t(lang, "nav_take_me"))}">'
            f'<p class="nav-banner-title">{_esc(_t(lang, "nav_take_me"))}</p>'
            f'<div class="nav-banner-btns">'
            f'<a class="nav-btn gmaps" href="{gmaps}" target="_blank" '
            f'rel="noopener">Google Maps</a>'
            f'<a class="nav-btn waze" href="{waze}" target="_blank" '
            f'rel="noopener">Waze</a>'
            f'</div></div>')


# ── Adresse & GPS copiables (M-19) : dans A_arrival, au-dessus des itinéraires ─
# L'adresse complète et les coordonnées GPS (position ajustée par le propriétaire,
# plus fiable que l'adresse en zone mal géocodée) sont affichées avec un bouton
# « Copier » (presse-papiers côté client, repli sélection). À coller dans un taxi,
# un covoiturage ou un GPS tiers.

def _gps_string(lat: Any, lon: Any) -> str:
    """Coordonnées « lat, lon » à 6 décimales (format universel taxi/GPS)."""
    return f"{float(lat):.6f}, {float(lon):.6f}"


def _address_string(prop: dict) -> str:
    """Adresse complète sur une ligne : voie, (complément), code postal + ville."""
    line1 = (prop.get("address_line1") or "").strip()
    line2 = (prop.get("address_line2") or "").strip()
    cp_city = " ".join(x for x in [(prop.get("postal_code") or "").strip(),
                                   (prop.get("city") or "").strip()] if x)
    return ", ".join(x for x in [line1, line2, cp_city] if x)


def _copy_row(label: str, value: str, lang: str) -> str:
    """Une ligne « libellé — valeur — bouton Copier » (data-copy pour le JS)."""
    v = _esc(value)
    return (f'<div class="copy-row">'
            f'<div class="cr-head"><span class="cr-label">{_esc(label)}</span>'
            f'<button class="copy-btn" type="button" data-copy="{v}" '
            f'data-copied="{_esc(_t(lang, "copied"))}">{_esc(_t(lang, "copy"))}</button></div>'
            f'<div class="cr-val" data-copy-value>{v}</div></div>')


def _render_arrival_meta(prop: dict, lang: str = "fr") -> str:
    rows: list[str] = []
    address = _address_string(prop)
    if address:
        rows.append(_copy_row(_t(lang, "address"), address, lang))
    if prop.get("lat") is not None and prop.get("lon") is not None:
        rows.append(_copy_row(_t(lang, "gps"),
                              _gps_string(prop["lat"], prop["lon"]), lang))
    if not rows:
        return ""
    return f'<div class="arrival-meta">{"".join(rows)}</div>'


def _render_transport(pois: list[dict], home_lat: Any, home_lon: Any,
                      lang: str = "fr") -> str:
    if not pois or home_lat is None or home_lon is None:
        return ""
    home = _latlon(home_lat, home_lon)
    trips: list[str] = []
    for p in pois:
        name = _esc(p["name"])
        drive = p.get("drive_min")
        dur = (f'<span class="trip-dur">{_esc(str(drive))} {_esc(_t(lang, "drive"))}</span>'
               if drive is not None else "")
        # Itinéraire de PLANIFICATION Google Maps : origine lieu → logement.
        if p.get("lat") is not None and p.get("lon") is not None:
            gmaps = (f"https://www.google.com/maps/dir/?api=1&origin={_latlon(p['lat'], p['lon'])}"
                     f"&destination={home}")
        else:  # sans coordonnées du POI, on laisse Google demander l'origine
            gmaps = f"https://www.google.com/maps/dir/?api=1&destination={home}"
        # Un seul bouton : Waze (pas d'origine) serait redondant avec le bandeau.
        btn = (f'<a class="trip-btn gmaps" href="{gmaps}" target="_blank" '
               f'rel="noopener">{_esc(_t(lang, "view_route"))}</a>')
        trips.append(
            f'<div class="trip"><div class="trip-head"><b>{name}</b>{dur}</div>'
            f'<div class="trip-btns">{btn}</div></div>')
    return (f'<div class="transport" aria-label="{_esc(_t(lang, "nav_to_home"))}">'
            f'{"".join(trips)}</div>')


# ── Section complète ─────────────────────────────────────────────────────────

def _render_section(sec: dict, contact: dict, tourism_license: str | None,
                    area_facts: dict | None = None, arrival: dict | None = None,
                    lang: str = "fr") -> str:
    schema = sec.get("field_schema") or {}
    content = sec.get("content") or {}
    title = _esc(_i18n(sec.get("name_i18n"), lang, sec.get("code", "")))
    parts: list[str] = [f"<h3>{title}</h3>"]

    # Section d'arrivée (déclare airport/train_station) : bandeau de navigation
    # universelle (M-20), puis adresse & GPS copiables (M-19), puis blocs de
    # planification par lieu (M-14) — rendus en tête, le texte libre du
    # propriétaire suit.
    if arrival and (set(schema.get("poi_categories") or []) & _TRANSPORT_CATEGORIES):
        banner_html = _render_nav_banner(arrival.get("lat"), arrival.get("lon"), lang)
        if banner_html:
            parts.append(banner_html)
        meta_html = _render_arrival_meta(arrival.get("prop") or {}, lang)
        if meta_html:
            parts.append(meta_html)
        trans_html = _render_transport(arrival.get("pois") or [],
                                       arrival.get("lat"), arrival.get("lon"), lang)
        if trans_html:
            parts.append(trans_html)

    fields_html = _render_fields(schema, content, lang)
    if fields_html:
        parts.append(fields_html)

    body_html = _md_to_html(sec.get("body_md"))
    if body_html:
        parts.append(f'<div class="prose">{body_html}</div>')

    # Faits locaux déclarés par la section (M-17) : tri, bruit… rendus sous les
    # champs du propriétaire, dans un encart sobre. Les numéros utiles restent
    # dans le bloc de fin de guide (jamais ici).
    facts_html = _render_section_facts(schema.get("area_facts"),
                                       area_facts or {}, lang)
    if facts_html:
        parts.append(f'<div class="sec-facts">{facts_html}</div>')

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
    # `id` = code de section (V2-09) : les ancres profondes `#<code>` mènent au
    # bon onglet (résolu côté client) et défilent jusqu'à la section.
    sec_id = _esc(sec.get("code") or "")
    return (f'<article class="sec-card" id="{sec_id}">'
            f'{"".join(p for p in parts if p)}</article>')


# ── POI d'un chapitre, groupés par catégorie, triés par distance ─────────────

def _render_pois(pois: list[dict], lang: str = "fr") -> str:
    if not pois:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for p in pois:
        by_cat.setdefault(p["category_code"], []).append(p)
    blocks: list[str] = []
    for code, lst in by_cat.items():
        # Coups de cœur (owner_comment) en tête de leur catégorie (M-16), puis
        # tri par distance à pied.
        lst.sort(key=lambda p: (
            0 if (p.get("owner_comment") or "").strip() else 1,
            p.get("dist_walk_m") if p.get("dist_walk_m") is not None else 9e9))
        cat_name = _esc(_i18n(lst[0].get("category_name"), lang, code))
        is_resto = code == "restaurant"
        cards: list[str] = []
        for p in lst:
            n, u = _fmt_dist(p, lang)
            color = _esc(p.get("map_color") or "#0E5A73")
            desc = _md_to_html(p.get("description_md")) if p.get("description_md") else ""
            comment = (f'<p class="fav">❤ {_esc(p["owner_comment"])}</p>'
                       if p.get("owner_comment") else "")
            hours = (f'<div class="hours">{_esc(p["opening_hours"])}</div>'
                     if p.get("opening_hours") else "")
            # Type de cuisine (M-16) : étiquette localisée + attribut de filtrage.
            cuisine = (p.get("cuisine") or "").strip().lower()
            cuisine_attr = f' data-cuisine="{_esc(cuisine)}"' if is_resto else ""
            cuisine_tag = (f'<span class="cuisine-tag">{_esc(_cuisine_label(cuisine, lang))}</span>'
                           if is_resto and cuisine else "")
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
                f'<div class="poi-card"{cuisine_attr} style="border-left-color:{color}">'
                f'<div class="dist"><b>{_esc(n)}</b><span>{_esc(u)}</span></div>'
                f'<div class="poi-body"><h4>{_esc(p["name"])}{cuisine_tag}</h4>{comment}'
                f'{f"<div class=prose>{desc}</div>" if desc else ""}{hours}{meta_html}</div></div>')
        n = len(lst)
        head = f'<h4 class="cat-title">{cat_name} · {n}</h4>'
        group = f'<div class="poi-group" data-cat="{_esc(code)}">{"".join(cards)}</div>'
        # Liste repliée (V2-09) : 4 cartes visibles, le reste sous « Voir les N
        # autres ». Rendu SSR (bouton masqué par défaut : sans JS toutes les cartes
        # restent visibles, dégradation acceptable). Le gabarit `{n}` est réinjecté
        # côté client (le compte change avec le filtre par cuisine des restaurants).
        more = ""
        if n > _POI_VISIBLE:
            tpl = _t(lang, "show_more")
            more = (f'<button class="more-btn" type="button" '
                    f'data-more-tpl="{_esc(tpl)}" data-less="{_esc(_t(lang, "show_less"))}">'
                    f'{_esc(tpl.format(n=n - _POI_VISIBLE))}</button>')
        chips = _render_cuisine_chips(lst, lang) if is_resto else ""
        blocks.append(f'<div class="cat" data-cat="{_esc(code)}">'
                      f'{head}{chips}{group}{more}</div>')
    return "".join(blocks)


def _render_cuisine_chips(restaurants: list[dict], lang: str) -> str:
    """Puces de filtre par cuisine (M-16), dérivées des valeurs réellement
    présentes. Libellés localisés (dictionnaire) avec repli sur la valeur brute.
    Aucune puce si moins de deux cuisines distinctes (le filtre n'aurait pas de
    sens)."""
    values = sorted({(p.get("cuisine") or "").strip().lower()
                     for p in restaurants if (p.get("cuisine") or "").strip()},
                    key=lambda v: _cuisine_label(v, lang).lower())
    if len(values) < 2:
        return ""
    chips = [f'<button class="cchip on" data-cuisine="">{_esc(_t(lang, "all"))}</button>']
    for v in values:
        chips.append(f'<button class="cchip" data-cuisine="{_esc(v)}">'
                     f'{_esc(_cuisine_label(v, lang))}</button>')
    return (f'<div class="cuisines" data-cat="restaurant" '
            f'aria-label="{_esc(_t(lang, "cuisine_filter"))}">{"".join(chips)}</div>')


# ── Barre d'urgences (numéros prioritaires, tel:) ────────────────────────────

def _render_sos(area_facts: dict, big: bool = False) -> str:
    """Barre d'urgences tactile (numéros prioritaires, `tel:`). En version
    compacte, elle reste en tête des TROIS onglets (§V2-09 : vital, ne se range
    pas) ; en version `big`, elle ouvre l'onglet « Urgences »."""
    items = ((area_facts.get("emergency_numbers") or {}).get("items") or [])[:4]
    if not items:
        return ""
    cells: list[str] = []
    for it in items:
        num = str(it.get("number", ""))
        cells.append(f'<a class="sos-item" href="tel:{_tel(num)}">'
                     f'<span class="num">{_esc(num)}</span>'
                     f'<span class="lbl">{_esc(it.get("label", ""))}</span></a>')
    cls = "sos sos-lg" if big else "sos"
    return f'<div class="{cls}">{"".join(cells)}</div>'


# ── Faits locaux (area_facts) rendus à leur place (M-17) ─────────────────────
# Chaque area_fact est rendu DANS la section qui le déclare (field_schema.
# area_facts) — waste_rules → C_trash, noise_rules → B_house_rules — sous les
# champs du propriétaire, dans un encart sobre. Seuls les numéros utiles restent
# regroupés dans un bloc de fin de guide (ils ne relèvent d'aucune section
# éditée par le propriétaire). Le contenu est généré en français par le pipeline ;
# seuls les intitulés de rubrique sont localisés (traduction du contenu : V2).

def _fact_waste(waste: dict, lang: str) -> str:
    """Encart « tri des déchets » (couleurs de conteneurs) rendu dans C_trash."""
    if not waste:
        return ""
    containers = "".join(
        f'<li><b>{_esc(c.get("color_or_type", ""))}</b> — {_esc(c.get("accepts", ""))}</li>'
        for c in (waste.get("containers") or []))
    return (f'<div class="facts"><b class="tt">{_t(lang, "waste")}</b>'
            f'<p>{_esc(waste.get("summary", ""))}</p>'
            f'{f"<ul>{containers}</ul>" if containers else ""}</div>')


def _fact_noise(noise: dict, lang: str) -> str:
    """Encart « tranquillité du voisinage » (heures de silence) → B_house_rules."""
    if not noise:
        return ""
    quiet = noise.get("quiet_hours")
    return (f'<div class="facts"><b class="tt">{_t(lang, "noise")}</b>'
            f'<p>{_esc(noise.get("summary", ""))}</p>'
            f'{f"<span class=quiet>🌙 {_esc(quiet)}</span>" if quiet else ""}</div>')


# Renderers d'encart par type de fait, adossés à une section (M-17). Les
# `emergency_numbers` n'y figurent PAS : ils restent dans le bloc de fin de guide.
_FACT_INLINE = {"waste_rules": _fact_waste, "noise_rules": _fact_noise}


def _render_section_facts(area_facts_declared: list, area_facts: dict,
                          lang: str) -> str:
    """Encarts des area_facts déclarés par une section (M-17), dans l'ordre du
    field_schema. `emergency_numbers` est ignoré ici (bloc de fin de guide)."""
    out: list[str] = []
    for key in area_facts_declared or []:
        render = _FACT_INLINE.get(key)
        if render:
            html_ = render(area_facts.get(key) or {}, lang)
            if html_:
                out.append(html_)
    return "".join(out)


def _render_numbers(area_facts: dict, chapter_color: str, lang: str = "fr") -> str:
    """Bloc de fin de guide (M-17) : UNIQUEMENT la liste complète des numéros
    utiles. Les autres faits (tri, bruit) sont désormais dans leur section."""
    emerg = area_facts.get("emergency_numbers")
    if not emerg or not emerg.get("items"):
        return ""
    nums = "".join(f'<li><b>{_esc(str(i.get("number", "")))}</b> — {_esc(i.get("label", ""))}</li>'
                   for i in emerg["items"])
    notes = emerg.get("notes")
    card = (f'<div class="facts"><b class="tt">{_t(lang, "numbers")}</b>'
            f'<ul>{nums}</ul>{f"<p class=fnote>{_esc(notes)}</p>" if notes else ""}</div>')
    return (f'<section class="chapter"><h2>{_t(lang, "good_to_know")}</h2>'
            f'<div class="chapline" style="background:{chapter_color}"></div>'
            f'{card}</section>')


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


def slugify(name: str | None, maxlen: int = 60) -> str:
    """Fragment lisible et sûr pour l'URL de partage (M-25) : « Villa Mar Azul »
    → « villa-mar-azul ». **Décoratif** : seul le token final fait foi côté
    serveur (le slug est ignoré à la lecture)."""
    ascii_name = (unicodedata.normalize("NFKD", name or "")
                  .encode("ascii", "ignore").decode())
    s = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
    return (s[:maxlen].strip("-")) or "guide"


def share_path(name: str | None, token: str) -> str:
    """Chemin de partage élégant `/g/{slug}-{token}` (M-25). L'ancien lien nu
    `/g/{token}` reste valide à jamais (le slug est décoratif)."""
    slug = slugify(name)
    return f"/g/{slug}-{token}"


def _og_tags(*, title: str, desc: str, url: str, image: str | None,
             locale: str) -> str:
    """Balises Open Graph + Twitter Card (M-25) : vignette de partage dans
    WhatsApp/iMessage/e-mail. `noindex` est conservé par ailleurs (§8)."""
    tags = [
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="CasaGuide">',
        f'<meta property="og:title" content="{_esc(title)}">',
        f'<meta property="og:description" content="{_esc(desc)}">',
        f'<meta property="og:locale" content="{_esc(locale)}">',
        f'<meta name="twitter:title" content="{_esc(title)}">',
        f'<meta name="twitter:description" content="{_esc(desc)}">',
    ]
    if url:
        tags.append(f'<meta property="og:url" content="{_esc(url)}">')
    if image:
        tags.append(f'<meta property="og:image" content="{_esc(image)}">')
        tags.append(f'<meta property="og:image:alt" content="{_esc(title)}">')
        tags.append(f'<meta name="twitter:image" content="{_esc(image)}">')
        tags.append('<meta name="twitter:card" content="summary_large_image">')
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    return "\n".join(tags)


def render_guide(prop: dict, sections: list[dict], pois: list[dict],
                 area_facts: dict, token: str, lang: str = "fr", *,
                 base_url: str = "", og_image_url: str | None = None) -> str:
    contact = prop.get("contact") or {}
    name = _esc(prop.get("name") or _t(lang, "home"))
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)

    # Trajets d'arrivée (M-14) : POI transport rendus en blocs dans A_arrival (et
    # retirés des listes ordinaires, anti-doublon). Repli en cartes si A_arrival
    # masquée — inchangé.
    transport_pois = [p for p in pois if p["category_code"] in _TRANSPORT_CATEGORIES]
    host_visible = any(set((s.get("field_schema") or {}).get("poi_categories") or [])
                       & _TRANSPORT_CATEGORIES for s in sections)
    arrival_ctx = ({"prop": prop, "pois": transport_pois, "lat": prop.get("lat"),
                    "lon": prop.get("lon")} if host_visible else None)

    def _chapter_card_pois(ch: str) -> list[dict]:
        cps = [p for p in pois if p["chapter"] == ch]
        if arrival_ctx and transport_pois:  # trajets rendus dans A_arrival
            cps = [p for p in cps
                   if p["category_code"] not in _TRANSPORT_CATEGORIES]
        return cps

    def _chapter_block(ch: str, inner: list[str], tab: str = "") -> str:
        parts = [x for x in inner if x]
        if not parts:
            return ""
        title = _i18n(_CHAPTER_TAB_NAMES.get((ch, tab)), lang,
                      _chapter_name(ch, lang)) if tab else _chapter_name(ch, lang)
        return (f'<section class="chapter" data-chapter="{ch}">'
                f'<h2>{_esc(title)}</h2>'
                f'<div class="chapline" style="background:{_CHAPTER_COLORS[ch]}"></div>'
                f'{"".join(parts)}</section>')

    # Répartition chapitre par chapitre dans les trois espaces (V2-09). Un chapitre
    # dont sections et POI vont au même espace produit UN bloc ; le chapitre C
    # (sections → « logement », commerces → « autour ») produit deux blocs.
    panels: dict[str, list[str]] = {"home": [], "emergency": [], "around": []}
    for ch in _CHAPTER_ORDER:
        poi_tab = _POI_TAB.get(ch, "home")
        default_tab = _SECTION_TAB.get(ch, "home")
        sec_by_tab: dict[str, list[str]] = {}
        for s in sections:
            if s["chapter"] != ch:
                continue
            tab = _SECTION_TAB_OVERRIDES.get(s.get("code"), default_tab)
            sec_by_tab.setdefault(tab, []).append(
                _render_section(s, contact, prop.get("tourism_license"),
                                area_facts, arrival_ctx, lang))
        pois_html = _render_pois(_chapter_card_pois(ch), lang)
        for tab in _TAB_ORDER:
            inner = list(sec_by_tab.get(tab, []))
            if tab == poi_tab and pois_html:
                inner.append(pois_html)
            blk = _chapter_block(ch, inner, tab=tab)
            if blk:
                panels[tab].append(blk)

    # Urgences : barre SOS EN GRAND + santé (chap. D, déjà réparti) + numéros utiles.
    big_sos = _render_sos(area_facts, big=True)
    numbers = _render_numbers(area_facts, _CHAPTER_COLORS["I"], lang)
    emergency_inner = (([big_sos] if big_sos else []) + panels["emergency"]
                       + ([numbers] if numbers else []))

    # Autour de vous : carte + puces de filtre (bâties sur les POI de cet espace).
    around_pois = [p for p in pois if _POI_TAB.get(p["chapter"], "home") == "around"]
    map_data = {
        "property": {"name": prop.get("name"), "lat": prop.get("lat"), "lon": prop.get("lon")},
        "pois": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                  "chapter": p["chapter"], "color": p.get("map_color"),
                  "category": _i18n(p.get("category_name"), lang, p["category_code"]),
                  "walk_min": p.get("walk_min"), "drive_min": p.get("drive_min"),
                  "phone": p.get("phone")}
                 for p in around_pois
                 if p.get("lat") is not None and p.get("lon") is not None],
    }
    data_json = json.dumps(map_data, ensure_ascii=False).replace("</", "<\\/")
    has_map = map_data["property"]["lat"] is not None

    around_chapters = [ch for ch in _CHAPTER_ORDER
                       if any(p["chapter"] == ch for p in around_pois)]
    chips = [f'<button class="chip on" data-chapter="">{_esc(_t(lang, "all"))}</button>']
    for ch in around_chapters:
        chip_name = _i18n(_CHAPTER_TAB_NAMES.get((ch, "around")), lang,
                          _chapter_name(ch, lang))
        chips.append(f'<button class="chip" data-chapter="{ch}">{_esc(chip_name)}</button>')
    around_inner: list[str] = []
    if has_map:
        around_inner.append('<div id="map"></div>')
    if around_chapters:
        around_inner.append(
            f'<nav class="chips" aria-label="{_esc(_t(lang, "filter"))}">{"".join(chips)}</nav>')
    around_inner += panels["around"]

    # Onglets + panneaux (V2-09). Sans JS, tous les panneaux restent visibles
    # (CSS gated sur `html.js`) → aucune perte de contenu (noscript = rouleau).
    _labels = {"home": _t(lang, "tab_home"), "emergency": _t(lang, "tab_emergency"),
               "around": _t(lang, "tab_around")}
    _inner = {"home": "".join(panels["home"]),
              "emergency": "".join(emergency_inner),
              "around": "".join(around_inner)}
    tabs_btns, panels_html = [], []
    for key in _TAB_ORDER:
        on = " on" if key == "home" else ""
        sel = "true" if key == "home" else "false"
        active = " tab-active" if key == "home" else ""
        tabs_btns.append(
            f'<button class="tab{on}" role="tab" data-tab="{key}" id="tabbtn-{key}" '
            f'aria-controls="tab-{key}" aria-selected="{sel}">{_esc(_labels[key])}</button>')
        panels_html.append(
            f'<section class="tab-panel{active}" data-tab="{key}" id="tab-{key}" '
            f'role="tabpanel" aria-labelledby="tabbtn-{key}">{_inner[key]}</section>')
    tabs_nav = (f'<nav class="guide-tabs" role="tablist" '
                f'aria-label="{_esc(_t(lang, "tabs"))}">{"".join(tabs_btns)}</nav>')

    sos = _render_sos(area_facts)
    default_lang = prop.get("default_lang") or "fr"
    langs = _render_langs(default_lang, prop.get("published_langs") or [], lang)

    # Liens de partage élégants (M-25) : vignette Open Graph. L'URL canonique de
    # partage porte le slug lisible (le token reste l'autorité).
    plain_name = prop.get("name") or _t(lang, "home")
    share_title = f"{plain_name} — {_t(lang, 'title_suffix')}"
    og_url = (base_url.rstrip("/") + share_path(prop.get("name"), token)) if base_url else ""
    og_html = _og_tags(title=share_title, desc=_t(lang, "share_desc"),
                       url=og_url, image=og_image_url,
                       locale=_OG_LOCALE.get(lang, "fr_FR"))

    return f"""<!DOCTYPE html>
<html lang="{_esc(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#0E5A73">
<script>document.documentElement.className += " js";</script>
<title>{name} — {_esc(_t(lang, "title_suffix"))}</title>
{og_html}
<link rel="manifest" href="/g/{_esc(token)}/manifest.webmanifest">
<link rel="apple-touch-icon" href="/guide/icon-192.png">
<link rel="icon" href="/guide/icon-192.png">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{versioned('/guide/guide.css')}">
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
  {tabs_nav}
  <main id="content">{"".join(panels_html)}</main>
  <footer>{_esc(_t(lang, "footer"))}</footer>
</div>
<script id="guide-data" type="application/json">{data_json}</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script type="module" src="{versioned('/guide/app.js')}"></script>
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
<link rel="stylesheet" href="{versioned('/guide/guide.css')}">
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
<link rel="stylesheet" href="{versioned('/guide/guide.css')}">
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
