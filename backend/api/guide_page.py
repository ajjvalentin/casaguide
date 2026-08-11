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

import datetime as _dt
import html
import json
import re
import unicodedata
from typing import Any

from . import i18n as _i18n_mod
from .assets import versioned
from .poi_icons import category_icon_svg, category_rank

# Noms de jours localisés (V2-33) : Babel/CLDR est l'équivalent SERVEUR d'un
# `Intl.DateTimeFormat(locale, {weekday:'long'})` — même source de vérité (CLDR),
# donc SSR et client rendent le même mot. Aucune clé i18n, 7 langues gratuites.
try:
    from babel.dates import format_date as _babel_format_date
except Exception:  # pragma: no cover — Babel est une dépendance déclarée
    _babel_format_date = None
# 2024-01-01 est un LUNDI (ISO weekday 1) → jour n = pivot + (n-1) jours.
_WEEKDAY_PIVOT = _dt.date(2024, 1, 1)

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
        "services_grid": "Services autour de vous",
        "back_services": "↑ Retour aux services", "back_top": "↑ Haut de page",
        "search_placeholder": "Rechercher dans le guide",
        "search_none": "Aucun résultat exact — suggestions :",
        "search_clear": "Effacer",
        "tabs": "Espaces du guide", "tab_home": "Le logement",
        "tab_emergency": "Urgences", "tab_around": "Autour de vous",
        "show_more": "Voir les {n} autres", "show_less": "Réduire",
        "nav_to_home": "Itinéraire vers le logement", "open_in": "Ouvrir dans",
        "nav_take_me": "Me guider vers le logement", "view_route": "Voir l'itinéraire",
        "go_there": "Y aller", "maps_apple": "Plans",
        "address": "Adresse", "gps": "Coordonnées GPS",
        "copy": "Copier", "copied": "Copié ✓",
        "title_suffix": "Guide du logement", "home": "Votre logement",
        "share_desc": "Tout pour votre séjour : arrivée, wifi, urgences, commerces, "
                      "restaurants et carte du quartier.",
        "footer": "Guide propulsé par Holaguia — données OpenStreetMap. Bon séjour !",
        "watermark": "Créé avec Holaguia",
        "request_service": "Demander ce service", "request_note": "Votre message (facultatif)",
        "request_send": "Envoyer la demande", "request_cancel": "Annuler",
        "request_sent": "Demande envoyée ✓",
        "request_intro": "Votre demande sera transmise à votre hôte, qui vous répondra.",
        "request_error": "Envoi impossible. Réessayez dans un instant.",
        "stay_hello": "Bonjour {name},", "stay_hello_generic": "Bienvenue !",
        "stay_dates": "Votre séjour du {start} au {end}",
        "showcase_banner": "Aperçu du guide",
        "example_tag": "exemple", "wifi_title": "Connexion Wifi",
        "wifi_network": "Réseau", "wifi_password": "Mot de passe",
        "keybox_title": "Boîte à clés", "keybox_code": "Code",
        "send_subject": "Votre guide — {property}",
        "send_hello": "Bonjour {name},", "send_hello_generic": "Bonjour,",
        "send_intro": "Voici le lien de votre guide :",
        "send_signoff": "Au plaisir de vous accueillir !",
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
        "services_grid": "Services around you",
        "back_services": "↑ Back to services", "back_top": "↑ Back to top",
        "search_placeholder": "Search the guide",
        "search_none": "No exact match — suggestions:",
        "search_clear": "Clear",
        "tabs": "Guide sections", "tab_home": "The home",
        "tab_emergency": "Emergencies", "tab_around": "Around you",
        "show_more": "Show {n} more", "show_less": "Show less",
        "nav_to_home": "Directions to the property", "open_in": "Open in",
        "nav_take_me": "Take me to the property", "view_route": "View route",
        "go_there": "Get directions", "maps_apple": "Apple Maps",
        "address": "Address", "gps": "GPS coordinates",
        "copy": "Copy", "copied": "Copied ✓",
        "title_suffix": "Property guide", "home": "Your accommodation",
        "share_desc": "Everything for your stay: check-in, wifi, emergencies, shops, "
                      "restaurants and a map of the area.",
        "footer": "Guide powered by Holaguia — OpenStreetMap data. Enjoy your stay!",
        "watermark": "Created with Holaguia",
        "request_service": "Request this service", "request_note": "Your message (optional)",
        "request_send": "Send request", "request_cancel": "Cancel",
        "request_sent": "Request sent ✓",
        "request_intro": "Your request will be sent to your host, who will get back to you.",
        "request_error": "Could not send. Please try again shortly.",
        "stay_hello": "Hello {name},", "stay_hello_generic": "Welcome!",
        "stay_dates": "Your stay from {start} to {end}",
        "showcase_banner": "Guide preview",
        "example_tag": "example", "wifi_title": "Wifi connection",
        "wifi_network": "Network", "wifi_password": "Password",
        "keybox_title": "Key box", "keybox_code": "Code",
        "send_subject": "Your guide — {property}",
        "send_hello": "Hello {name},", "send_hello_generic": "Hello,",
        "send_intro": "Here is the link to your guide:",
        "send_signoff": "Looking forward to welcoming you!",
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
        "services_grid": "Servicios a tu alrededor",
        "back_services": "↑ Volver a los servicios", "back_top": "↑ Volver arriba",
        "search_placeholder": "Buscar en la guía",
        "search_none": "Sin coincidencias exactas — sugerencias:",
        "search_clear": "Borrar",
        "tabs": "Espacios de la guía", "tab_home": "El alojamiento",
        "tab_emergency": "Emergencias", "tab_around": "A tu alrededor",
        "show_more": "Ver {n} más", "show_less": "Reducir",
        "nav_to_home": "Cómo llegar al alojamiento", "open_in": "Abrir en",
        "nav_take_me": "Llévame al alojamiento", "view_route": "Ver ruta",
        "go_there": "Cómo llegar", "maps_apple": "Mapas",
        "address": "Dirección", "gps": "Coordenadas GPS",
        "copy": "Copiar", "copied": "Copiado ✓",
        "title_suffix": "Guía del alojamiento", "home": "Tu alojamiento",
        "share_desc": "Todo para tu estancia: llegada, wifi, urgencias, comercios, "
                      "restaurantes y mapa del barrio.",
        "footer": "Guía con tecnología de Holaguia — datos de OpenStreetMap. ¡Feliz estancia!",
        "watermark": "Creado con Holaguia",
        "request_service": "Solicitar este servicio", "request_note": "Tu mensaje (opcional)",
        "request_send": "Enviar solicitud", "request_cancel": "Cancelar",
        "request_sent": "Solicitud enviada ✓",
        "request_intro": "Tu solicitud se enviará a tu anfitrión, que te responderá.",
        "request_error": "No se pudo enviar. Inténtalo de nuevo en un momento.",
        "stay_hello": "Hola {name},", "stay_hello_generic": "¡Bienvenido/a!",
        "stay_dates": "Tu estancia del {start} al {end}",
        "showcase_banner": "Vista previa de la guía",
        "example_tag": "ejemplo", "wifi_title": "Conexión Wifi",
        "wifi_network": "Red", "wifi_password": "Contraseña",
        "keybox_title": "Caja de llaves", "keybox_code": "Código",
        "send_subject": "Tu guía — {property}",
        "send_hello": "Hola {name}:", "send_hello_generic": "Hola:",
        "send_intro": "Aquí tienes el enlace de tu guía:",
        "send_signoff": "¡Te esperamos!",
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
    """Libellé localisé d'un type de cuisine (M-16). Overlay des langues publiées
    supplémentaires (V2-21a) d'abord, puis le code (FR/EN/ES), puis la valeur brute
    embellie (underscores → espaces, capitalisée)."""
    ov = _i18n_mod.overlaid(_i18n_mod.cuisine_key(value))
    if ov:
        return ov
    d = _CUISINE_LABELS.get(value)
    if d:
        return d.get(lang) or d.get("fr") or value
    return value.replace("_", " ").strip().capitalize()


def _weekday_label(weekday: Any, lang: str = "fr") -> str:
    """Nom du jour (1=lundi … 7=dimanche, ISO) localisé dans la langue du guide via
    Babel/CLDR — l'équivalent serveur d'`Intl.DateTimeFormat` (V2-33) : aucune clé
    i18n, 7 langues gratuites, aligné avec le client. Chaîne vide si le jour est
    absent/invalide ; repli anglais si la locale est inconnue ou Babel absent."""
    try:
        n = int(weekday)
    except (TypeError, ValueError):
        return ""
    if not 1 <= n <= 7 or _babel_format_date is None:
        return ""
    d = _WEEKDAY_PIVOT + _dt.timedelta(days=n - 1)
    try:
        return _babel_format_date(d, "EEEE", locale=lang)
    except Exception:                     # locale inconnue de CLDR → repli anglais
        try:
            return _babel_format_date(d, "EEEE", locale="en")
        except Exception:
            return ""


def _t(lang: str, key: str) -> str:
    """Libellé fixe de l'interface dans `lang`. Priorité à l'overlay des langues
    publiées supplémentaires (V2-21a, `ui_translations`), puis au code (FR/EN/ES),
    puis au français. Pour FR/EN/ES l'overlay est vide → rendu identique."""
    return (_i18n_mod.overlaid(_i18n_mod.ui_key(key))
            or _UI.get(lang, {}).get(key) or _UI["fr"][key])


def _seed_label(lang: str, key: str, i18n: Any, fallback: str = "") -> str:
    """Libellé de seed localisé (nom de section, catégorie de POI, chapitre par
    onglet…) : overlay des langues supplémentaires (V2-21a) d'abord, puis le
    `name_i18n`/`description_i18n` du seed (FR/EN/ES), puis `fallback`. `key` est
    la clé stable de l'inventaire (`api.i18n.section_name_key(...)` etc.)."""
    return _i18n_mod.overlaid(key) or _i18n(i18n, lang, fallback)


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

# Au-delà de ce temps à pied, une catégorie 'walking' (ex. plage) bascule quand
# même en voiture : rester cohérent (une plage à 90 min ne se fait pas à pied).
_WALK_MODE_MAX = 45


def _fmt_dist(poi: dict, lang: str = "fr") -> tuple[str, str]:
    """Distance « voyageur » (V2-24). Le mode de trajet préféré de la catégorie
    (`travel_mode`) prime sur l'auto :
      - 'driving'  : toujours en voiture, avec la distance (ex. station-service —
        on y va en voiture même à 400 m) ;
      - 'walking'  : à pied tant que raisonnable (≤ `_WALK_MODE_MAX`), sinon voiture ;
      - None        : auto historique (à pied si ≤ 30 min, sinon voiture).
    Repli propre si le temps du mode demandé manque (POI ancien)."""
    walk = poi.get("walk_min")
    drive = poi.get("drive_min")
    mode = poi.get("travel_mode")
    if mode == "driving" and drive is not None:
        return str(drive), _t(lang, "drive")
    if mode == "walking" and walk is not None and (drive is None or walk <= _WALK_MODE_MAX):
        return str(walk), _t(lang, "walk")
    if walk is not None and walk <= 30:
        return str(walk), _t(lang, "walk")
    if drive is not None:
        return str(drive), _t(lang, "drive")
    if walk is not None:
        return str(walk), _t(lang, "walk")
    return "–", ""


# ── Rendu des champs d'une section (selon field_schema) ──────────────────────

def _render_fields(schema: dict, content: dict, lang: str = "fr",
                   section_code: str = "") -> str:
    rows: list[str] = []
    for f in schema.get("fields", []):
        key = f.get("key")
        val = content.get(key)
        if val is None or val == "":
            continue
        # Libellé du champ : overlay des langues supplémentaires (V2-21a) d'abord
        # (clé scopée par section), puis le `label` du seed (FR/EN/ES), puis la clé
        # brute. Pour FR/EN/ES l'overlay est vide → rendu identique (non-régression).
        label = _esc(_seed_label(lang, _i18n_mod.field_label_key(section_code, key or ""),
                                 f.get("label"), key or ""))
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
            dd = _esc(_seed_label(lang, _i18n_mod.option_key(str(val)),
                                  _OPTION_LABELS.get(val), str(val)))
        elif typ == "textarea":
            dd = _md_to_html(str(val))
        else:
            dd = _esc(str(val))
        rows.append(f'<div class="frow"><dt>{label}</dt><dd>{dd}</dd></div>')

    # Groupes répétables (équipements, services…)
    repeat = schema.get("repeat")
    if repeat:
        rkey = repeat.get("key")
        arr = content.get(rkey) or []
        cards: list[str] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            inner: list[str] = []
            for rf in repeat.get("fields", []):
                rv = item.get(rf.get("key"))
                if rv is None or rv == "":
                    continue
                rlabel = _esc(_seed_label(
                    lang, _i18n_mod.repeat_field_label_key(
                        section_code, rkey or "", rf.get("key") or ""),
                    rf.get("label"), ""))
                inner.append(
                    f'<div class="frow"><dt>{rlabel}</dt>'
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


# ── Secrets d'exemple pour le lien vitrine (V2-23c) ──────────────────────────
# Le lien vitrine (`/v/…`) montre le vrai produit à un prospect SANS jamais
# révéler un secret réel : on remplace wifi/boîte à clés par des valeurs d'EXEMPLE
# explicitement marquées. Le rendu ne touche JAMAIS `property_secrets` (invariant
# V2-23c) — ces valeurs sont des littéraux, aucune donnée sensible ne transite.

def _example_row(label: str, value: str, lang: str, *, mono: bool = False) -> str:
    tag = f'<span class="sc-eg">— {_esc(_t(lang, "example_tag"))}</span>'
    vcls = "v mono" if mono else "v"
    return (f'<div class="sc-row"><span class="k">{_esc(label)}</span>'
            f'<span class="{vcls}">{_esc(value)} {tag}</span></div>')


def _example_wifi_card(lang: str) -> str:
    return (f'<div class="secret-card sc-example">'
            f'<div class="sc-title">📶 {_esc(_t(lang, "wifi_title"))}</div>'
            f'{_example_row(_t(lang, "wifi_network"), "Villa-Wifi", lang)}'
            f'{_example_row(_t(lang, "wifi_password"), "MotDePasseWifi", lang, mono=True)}'
            f'</div>')


def _example_keybox_card(lang: str) -> str:
    return (f'<div class="secret-card sc-example">'
            f'<div class="sc-title">🔑 {_esc(_t(lang, "keybox_title"))}</div>'
            f'{_example_row(_t(lang, "keybox_code"), "1234", lang, mono=True)}'
            f'</div>')


# ── Section complète ─────────────────────────────────────────────────────────

def _render_section(sec: dict, contact: dict, tourism_license: str | None,
                    area_facts: dict | None = None, arrival: dict | None = None,
                    lang: str = "fr", *, requests_enabled: bool = True,
                    secrets_example: bool = False) -> str:
    schema = sec.get("field_schema") or {}
    content = sec.get("content") or {}
    _sc = sec.get("code", "")
    title = _esc(_seed_label(lang, _i18n_mod.section_name_key(_sc),
                             sec.get("name_i18n"), _sc))
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

    fields_html = _render_fields(schema, content, lang, _sc)
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

    # Emplacements réservés aux secrets. Lien maison/séjour : slots remplis côté
    # client depuis /secrets (déchiffrement à la demande). Lien VITRINE (V2-23c) :
    # valeurs d'EXEMPLE rendues côté serveur (jamais de secret réel, jamais de slot
    # → pas de fetch de secrets).
    _secrets = schema.get("secrets") or []
    if "wifi_pass" in _secrets:
        parts.append(_example_wifi_card(lang) if secrets_example
                     else '<div class="secret-slot" data-secret="wifi" hidden></div>')
    if "keybox_code" in _secrets:
        parts.append(_example_keybox_card(lang) if secrets_example
                     else '<div class="secret-slot" data-secret="keybox" hidden></div>')

    parts.append(_render_media(sec.get("media") or [], lang))

    # Demander ce service (V2-23b, §3.1) : les sections « sur demande » (ménage/
    # draps supplémentaires, services) portent `field_schema.request`. Le bouton est
    # rendu côté serveur (lisible sans JS) et enrichi en formulaire par app.js ; le
    # POST vers /g/{token}/requests est une ACTION du voyageur (invariant 4 intact).
    req = schema.get("request")
    if requests_enabled and req and isinstance(req, dict):
        code = _esc(sec.get("code") or "")
        # Libellés localisés portés en data-* (source unique côté serveur, comme
        # data-copy/data-more-tpl) → app.js enrichit sans dupliquer l'i18n.
        parts.append(
            f'<div class="svc-request" data-section="{code}"'
            f' data-intro="{_esc(_t(lang, "request_intro"))}"'
            f' data-note="{_esc(_t(lang, "request_note"))}"'
            f' data-send="{_esc(_t(lang, "request_send"))}"'
            f' data-cancel="{_esc(_t(lang, "request_cancel"))}"'
            f' data-sent="{_esc(_t(lang, "request_sent"))}"'
            f' data-error="{_esc(_t(lang, "request_error"))}">'
            f'<button type="button" class="svc-request-btn">'
            f'{_t(lang, "request_service")}</button></div>')

    # `id` = code de section (V2-09) : les ancres profondes `#<code>` mènent au
    # bon onglet (résolu côté client) et défilent jusqu'à la section.
    sec_id = _esc(sec.get("code") or "")
    return (f'<article class="sec-card" id="{sec_id}">'
            f'{"".join(p for p in parts if p)}</article>')


# ── POI d'un chapitre, groupés par catégorie, triés par distance ─────────────

def _itinerary_links(lat: Any, lon: Any, lang: str = "fr") -> str:
    """Boutons d'itinéraire vers un POI (V2-24) : Google Maps / Waze / Apple Maps.
    Simples liens profonds ouverts par le voyageur (`target=_blank`) → aucun
    chargement automatique, aucun SDK : l'invariant 4 (zéro appel externe côté
    voyageur) reste intact."""
    if lat is None or lon is None:
        return ""
    dest = _esc(_latlon(lat, lon))
    google = f"https://www.google.com/maps/dir/?api=1&destination={dest}"
    waze = f"https://waze.com/ul?ll={dest}&navigate=yes"
    apple = f"https://maps.apple.com/?daddr={dest}"
    links = (
        f'<a class="route-link" href="{google}" target="_blank" rel="noopener">Google Maps</a>'
        f'<a class="route-link" href="{waze}" target="_blank" rel="noopener">Waze</a>'
        f'<a class="route-link" href="{apple}" target="_blank" rel="noopener">'
        f'{_esc(_t(lang, "maps_apple"))}</a>'
    )
    return (f'<div class="poi-nav">'
            f'<span class="nav-lbl">{_esc(_t(lang, "go_there"))}</span>{links}</div>')


def _render_pois(pois: list[dict], lang: str = "fr", tab_hash: str = "") -> str:
    """Rend les POI d'un chapitre en blocs `.cat` (un par catégorie). `tab_hash`
    (V2-12 : « autour », « logement »…) préfixe l'`id` d'ancre de chaque bloc
    (`id="autour/{code}"`) → cible des tuiles de la grille de services et des
    liens profonds, fonctionnelle même sans JS."""
    if not pois:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for p in pois:
        by_cat.setdefault(p["category_code"], []).append(p)
    blocks: list[str] = []
    # Ordre des catégories = celui du seed (V2-12) → cohérent avec la grille de
    # services (mêmes tuiles, même ordre) et avec l'intention du catalogue.
    for code, lst in sorted(by_cat.items(), key=lambda kv: category_rank(kv[0])):
        is_market = code == "market"
        if is_market:
            # V2-33 — un voyageur se repère par JOUR, pas par distance : les marchés
            # sont triés lundi→dimanche (jour absent en dernier), puis par distance.
            lst.sort(key=lambda p: (
                p.get("weekday") if p.get("weekday") is not None else 8,
                p.get("dist_walk_m") if p.get("dist_walk_m") is not None else 9e9))
        else:
            # Autres catégories : coup de cœur (owner_comment) en tête (M-16), puis
            # distance à pied — le repère naturel d'un lieu reste « à quelle distance ».
            lst.sort(key=lambda p: (
                0 if (p.get("owner_comment") or "").strip() else 1,
                p.get("dist_walk_m") if p.get("dist_walk_m") is not None else 9e9))
        cat_name = _esc(_seed_label(lang, _i18n_mod.poi_category_key(code),
                                    lst[0].get("category_name"), code))
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
            # Jour du marché (V2-33) : badge TRADUIT (Babel/CLDR) dans la langue du
            # guide + précision libre à côté. `data-weekday` porte le rang (1..7) →
            # le client (popups carte, app.js) rend le MÊME mot via Intl (aligné).
            day_html = ""
            if is_market and p.get("weekday"):
                day_txt = _weekday_label(p.get("weekday"), lang)
                note = (p.get("weekday_note") or "").strip()
                note_html = (f'<span class="market-note">{_esc(note)}</span>'
                             if note else "")
                day_html = (f'<div class="market-day" data-weekday="{int(p["weekday"])}">'
                            f'<span class="market-badge">{_esc(day_txt)}</span>'
                            f'{note_html}</div>')
            meta: list[str] = []
            if p.get("phone"):
                meta.append(f'<a href="tel:{_tel(p["phone"])}">{_t(lang, "call")}</a>')
            if p.get("website"):
                meta.append(f'<a href="{_esc(p["website"])}" target="_blank" rel="noopener nofollow">{_t(lang, "website")}</a>')
            meta_html = f'<div class="meta">{"".join(meta)}</div>' if meta else ""
            # Itinéraire (V2-24) : Google Maps / Waze / Apple Maps sur TOUS les POI
            # géolocalisés (remplace l'ancien lien Google unique).
            nav_html = _itinerary_links(p.get("lat"), p.get("lon"), lang)
            cards.append(
                f'<div class="poi-card"{cuisine_attr} style="border-left-color:{color}">'
                f'<div class="dist"><b>{_esc(n)}</b><span>{_esc(u)}</span></div>'
                f'<div class="poi-body"><h4>{_esc(p["name"])}{cuisine_tag}</h4>{day_html}{comment}'
                f'{f"<div class=prose>{desc}</div>" if desc else ""}{hours}{meta_html}{nav_html}</div></div>')
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
        # Ancre profonde (V2-12) : id = « {onglet}/{code} » → cible des tuiles de
        # la grille (`#autour/{code}`) et lien natif fonctionnel sans JS.
        anchor = f' id="{_esc(tab_hash)}/{_esc(code)}"' if tab_hash else ""
        # Retour aux services (V2-27) : en fin de CHAQUE catégorie de « Autour de
        # vous », un lien d'ancre vers la grille (`#autour`) — chemin de retour
        # visible, fonctionnel même sans JS (l'`id` de la grille est « autour »).
        back = (f'<a class="back-services" href="#{_TAB_HASH["around"]}">'
                f'{_esc(_t(lang, "back_services"))}</a>'
                if tab_hash == _TAB_HASH["around"] else "")
        blocks.append(f'<div class="cat" data-cat="{_esc(code)}"{anchor}>'
                      f'{head}{chips}{group}{more}{back}</div>')
    return "".join(blocks)


def _render_service_grid(pois: list[dict], lang: str = "fr") -> str:
    """Grille de pictogrammes en tête de « Autour de vous » (V2-12).

    Une tuile par catégorie ayant ≥1 POI retenu : grande icône du seed
    (`poi_categories.icon`, rendue en SVG inline par `poi_icons`), nom localisé
    et compte. C'est la **navigation principale** de l'onglet : chaque tuile est
    un lien d'ancre `#autour/{code}` vers le bloc de la catégorie (dont l'`id`
    est justement `autour/{code}`) → fonctionne même sans JS ; l'enrichissement
    client (app.js) résout l'onglet + le défilement doux + le retour arrière.

    Ordre : celui du seed (`poi_icons.category_rank`), cohérent avec le reste du
    guide. Aucune tuile si aucun POI (repli : rien, la carte/les listes suffisent)."""
    if not pois:
        return ""
    groups: dict[str, list[dict]] = {}
    for p in pois:
        groups.setdefault(p["category_code"], []).append(p)
    tiles: list[str] = []
    for code, lst in sorted(groups.items(), key=lambda kv: category_rank(kv[0])):
        name = _esc(_seed_label(lang, _i18n_mod.poi_category_key(code),
                                lst[0].get("category_name"), code))
        icon = category_icon_svg(code, lst[0].get("category_icon"))
        count = len(lst)
        color = _esc(lst[0].get("map_color") or "#0E5A73")
        tiles.append(
            f'<a class="svc-tile" href="#{_TAB_HASH["around"]}/{_esc(code)}" '
            f'data-cat="{_esc(code)}" style="--svc-accent:{color}" '
            f'aria-label="{name} : {count}">'
            f'<span class="svc-ic">{icon}</span>'
            f'<span class="svc-name">{name}</span>'
            f'<span class="svc-count">{count}</span></a>')
    # id = « autour » (V2-27) : cible du retour aux services. Comme les liens de
    # retour pointent `#autour`, le navigateur défile nativement jusqu'à la grille
    # (sans JS) ; le hash reste propre (`#autour`, l'onglet), historique cohérent.
    return (f'<nav class="svc-grid" id="{_TAB_HASH["around"]}" '
            f'aria-label="{_esc(_t(lang, "services_grid"))}">'
            f'{"".join(tiles)}</nav>')


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


def _fact_food_delivery(fd: dict, lang: str) -> str:
    """Encart « livraison de repas » → section E_food_delivery (V2-07 volet 1).
    Liste les plateformes actives dans la zone, sous leur marque locale — NOMS
    NEUTRES (jamais traduits) avec lien vers la preuve. Prose minimale (`note`).
    Rien à afficher si aucune plateforme (liste vide = résultat valide)."""
    if not fd:
        return ""
    items: list[str] = []
    for p in (fd.get("platforms") or []):
        name = _esc((p.get("name") or "").strip())
        if not name:
            continue
        url = (p.get("url") or "").strip()
        if url.lower().startswith(("http://", "https://")):
            items.append(f'<li><a href="{_esc(url)}" target="_blank" '
                         f'rel="noopener nofollow">{name}</a></li>')
        else:
            items.append(f'<li>{name}</li>')
    if not items:
        return ""
    note = (fd.get("note") or "").strip()
    note_html = f'<p class="fnote">{_esc(note)}</p>' if note else ""
    return (f'<div class="facts food-delivery">'
            f'<ul class="fd-list">{"".join(items)}</ul>{note_html}</div>')


# Renderers d'encart par type de fait, adossés à une section (M-17). Les
# `emergency_numbers` n'y figurent PAS : ils restent dans le bloc de fin de guide.
_FACT_INLINE = {"waste_rules": _fact_waste, "noise_rules": _fact_noise,
                "food_delivery": _fact_food_delivery}


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
                  current_lang: str, lang_names: dict | None = None) -> str:
    """Sélecteur de langue : la langue source + les langues publiées, MAIS bornées
    au registre des langues (V2-21a) — `lang_names` (code→nom natif) est la carte
    des langues publiées du produit. Une langue absente du registre n'apparaît
    jamais, même si elle reste dans `published_langs` du logement. Chaque entrée
    est un lien `?lang=xx` (rendu serveur) ; l'app mémorise le choix (localStorage)
    et détecte `navigator.language` au premier chargement à partir de ces liens."""
    names = lang_names or {}
    # La langue source est toujours offerte ; les autres seulement si publiées ET
    # dans le registre (clés de `names`).
    langs = [default_lang] + [l for l in (published_langs or [])
                              if l != default_lang and l in names]
    if len(langs) <= 1:
        return ""  # une seule langue → pas de sélecteur
    btns = []
    for l in langs:
        active = " on" if l == current_lang else ""
        aria = ' aria-current="true"' if l == current_lang else ""
        href = "?lang=" + _esc(l) if l != default_lang else "?lang=" + _esc(default_lang)
        label = names.get(l) or _LANG_LABELS.get(l, l.upper())
        btns.append(f'<a class="lang{active}" href="{href}" data-lang="{_esc(l)}"{aria} '
                    f'title="{_esc(label)}">{_esc(l.upper())}</a>')
    return f'<div class="langs" aria-label="{_t(current_lang, "lang")}">{"".join(btns)}</div>'


# ── Page complète ────────────────────────────────────────────────────────────

def _chapter_name(ch: str, lang: str) -> str:
    """Nom localisé d'un chapitre. Overlay des langues supplémentaires (V2-21a)
    d'abord, puis le code (FR/EN/ES), puis le repli français."""
    return (_i18n_mod.overlaid(_i18n_mod.chapter_key(ch))
            or _CHAPTER_NAMES.get(lang, {}).get(ch)
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
        '<meta property="og:site_name" content="Holaguia">',
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


def _watermark_html(lang: str) -> str:
    """Pied de page discret « Créé avec Holaguia » du plan gratuit (V2-05a).
    Simple lien vers holaguia.com — aucun appel externe (invariant 4)."""
    return (f'<div class="watermark">'
            f'<a href="https://holaguia.com" target="_blank" rel="noopener">'
            f'{_esc(_t(lang, "watermark"))}</a></div>')


def render_guide(prop: dict, sections: list[dict], pois: list[dict],
                 area_facts: dict, token: str, lang: str = "fr", *,
                 base_url: str = "", og_image_url: str | None = None,
                 watermark: bool = False, lang_names: dict | None = None,
                 ui_overlay: dict | None = None, variant: str = "house",
                 stay: dict | None = None, canonical_path: str | None = None,
                 manifest: bool = True, api_base: str | None = None) -> str:
    """Rend la page du guide dans `lang`. `ui_overlay` (V2-21a) = carte
    {clé: texte} des libellés statiques traduits pour une langue publiée
    supplémentaire (nl/de/it/sq…) ; vide pour FR/EN/ES (rendu depuis le code).
    Posé dans un ContextVar le temps du rendu, restauré ensuite.

    Trois liens (V2-23c) via `variant` :
      - `house`    : lien maison (QR imprimé), comportement historique ;
      - `stay`     : lien de séjour — accueil personnalisé (`stay`={guest_name,
        starts_on, ends_on}), demandes rattachées au séjour ;
      - `showcase` : lien vitrine — secrets d'EXEMPLE (jamais réels), demandes
        désactivées, bandeau « Aperçu du guide ».
    `canonical_path` surcharge l'URL canonique (og:url) — utile pour la vitrine
    (`/v/{token}`) et le séjour (`/b/{token}`). `manifest=False` retire le lien
    de manifeste (vitrine + séjour).

    `api_base` (V2-23c volet 1bis) = préfixe de TOUS les appels du client (secrets,
    demandes, médias, og) — `/g/{guide_token}` (maison), `/b/{stay_token}` (séjour),
    `/v/{showcase_token}` (vitrine). C'est l'UNIQUE source de vérité du DOM : posé
    en `data-api-base`, il garantit que le `guide_token` éternel ne transite jamais
    par la page d'un lien de séjour (le token de l'URL désigne à lui seul l'API).
    Défaut : `/g/{token}` (rétrocompat)."""
    _ov_tok = _i18n_mod.set_overlay(ui_overlay)
    try:
        return _render_guide_impl(prop, sections, pois, area_facts, token, lang,
                                  base_url=base_url, og_image_url=og_image_url,
                                  watermark=watermark, lang_names=lang_names,
                                  variant=variant, stay=stay,
                                  canonical_path=canonical_path,
                                  manifest=manifest,
                                  api_base=api_base or f"/g/{token}")
    finally:
        _i18n_mod.reset_overlay(_ov_tok)


def _fmt_date_num(d: Any) -> str:
    """Date en JJ/MM/AAAA (numérique, neutre en langue). Repli sûr si absente."""
    try:
        return d.strftime("%d/%m/%Y")
    except Exception:  # noqa: BLE001 — jamais bloquant sur un accueil
        return str(d or "")


def _stay_welcome_html(stay: dict, lang: str) -> str:
    """Accueil personnalisé du lien de séjour (V2-23c, §1.3) : « Bonjour {prénom},
    votre séjour du {arrivée} au {départ} ». Accueil générique si le nom manque."""
    name = (stay.get("guest_name") or "").strip()
    first = name.split()[0] if name else ""
    hello = (_t(lang, "stay_hello").format(name=_esc(first)) if first
             else _esc(_t(lang, "stay_hello_generic")))
    dates = _esc(_t(lang, "stay_dates")).format(
        start=_esc(_fmt_date_num(stay.get("starts_on"))),
        end=_esc(_fmt_date_num(stay.get("ends_on"))))
    return (f'<div class="stay-welcome"><span class="sw-hi">{hello}</span> '
            f'<span class="sw-dates">{dates}</span></div>')


def _render_guide_impl(prop: dict, sections: list[dict], pois: list[dict],
                       area_facts: dict, token: str, lang: str = "fr", *,
                       base_url: str = "", og_image_url: str | None = None,
                       watermark: bool = False, lang_names: dict | None = None,
                       variant: str = "house", stay: dict | None = None,
                       canonical_path: str | None = None,
                       manifest: bool = True, api_base: str = "") -> str:
    secrets_example = variant == "showcase"
    requests_enabled = variant != "showcase"
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
        title = (_seed_label(lang, _i18n_mod.chapter_tab_key(ch, tab),
                             _CHAPTER_TAB_NAMES.get((ch, tab)), _chapter_name(ch, lang))
                 if tab else _chapter_name(ch, lang))
        return (f'<section class="chapter" data-chapter="{ch}">'
                f'<h2>{_esc(title)}</h2>'
                f'<div class="chapline" style="background:{_CHAPTER_COLORS[ch]}"></div>'
                f'{"".join(parts)}</section>')

    # Répartition chapitre par chapitre dans les trois espaces (V2-09). Un chapitre
    # dont sections et POI vont au même espace produit UN bloc ; le chapitre C
    # (sections → « logement », commerces → « autour ») produit deux blocs.
    panels: dict[str, list[str]] = {"home": [], "emergency": [], "around": []}
    # POI réellement rendus en cartes dans « Autour de vous » (V2-12) : sert de
    # source à la grille de services (mêmes POI, même ordre → tuiles ↔ blocs 1:1).
    around_card_pois: list[dict] = []
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
                                area_facts, arrival_ctx, lang,
                                requests_enabled=requests_enabled,
                                secrets_example=secrets_example))
        chapter_card_pois = _chapter_card_pois(ch)
        if poi_tab == "around":
            around_card_pois.extend(chapter_card_pois)
        pois_html = _render_pois(chapter_card_pois, lang, tab_hash=_TAB_HASH[poi_tab])
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
                  "chapter": p["chapter"], "category_code": p["category_code"],
                  "color": p.get("map_color"),
                  "category": _seed_label(lang, _i18n_mod.poi_category_key(p["category_code"]),
                                          p.get("category_name"), p["category_code"]),
                  "walk_min": p.get("walk_min"), "drive_min": p.get("drive_min"),
                  "travel_mode": p.get("travel_mode"), "phone": p.get("phone"),
                  # Jour du marché (V2-33) : le client rend le badge localisé (Intl)
                  # dans les popups de la carte, aligné sur le SSR (Babel/CLDR).
                  "weekday": p.get("weekday"), "weekday_note": p.get("weekday_note")}
                 for p in around_pois
                 if p.get("lat") is not None and p.get("lon") is not None],
    }
    data_json = json.dumps(map_data, ensure_ascii=False).replace("</", "<\\/")
    has_map = map_data["property"]["lat"] is not None

    around_chapters = [ch for ch in _CHAPTER_ORDER
                       if any(p["chapter"] == ch for p in around_pois)]
    chips = [f'<button class="chip on" data-chapter="">{_esc(_t(lang, "all"))}</button>']
    for ch in around_chapters:
        chip_name = _seed_label(lang, _i18n_mod.chapter_tab_key(ch, "around"),
                                _CHAPTER_TAB_NAMES.get((ch, "around")),
                                _chapter_name(ch, lang))
        chips.append(f'<button class="chip" data-chapter="{ch}">{_esc(chip_name)}</button>')
    around_inner: list[str] = []
    # Grille de services (V2-12) EN TÊTE de l'onglet, avant la carte : c'est la
    # navigation principale (sur mobile en plein soleil, une grille d'icônes bat
    # dix intitulés texte). La carte + les puces de filtre restent la couche
    # d'exploration en dessous, les listes le niveau 2.
    grid_html = _render_service_grid(around_card_pois, lang)
    if grid_html:
        around_inner.append(grid_html)
    if has_map:
        around_inner.append('<div id="map"></div>')
    if around_chapters:
        around_inner.append(
            f'<nav class="chips" aria-label="{_esc(_t(lang, "filter"))}">{"".join(chips)}</nav>')
    around_inner += panels["around"]
    # Bouton flottant « Retour aux services » (V2-27) : rendu SSR mais masqué par
    # défaut (bonus JS) — app.js le révèle quand on défile dans « Autour de vous ».
    # N'a de sens que s'il y a une grille (des POI en cartes autour).
    back_float = (f'<a class="back-services back-float" href="#{_TAB_HASH["around"]}" '
                  f'aria-label="{_esc(_t(lang, "back_services"))}">'
                  f'{_esc(_t(lang, "back_services"))}</a>') if around_card_pois else ""

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
    langs = _render_langs(default_lang, prop.get("published_langs") or [], lang,
                          lang_names)

    # Liens de partage élégants (M-25) : vignette Open Graph. L'URL canonique de
    # partage porte le slug lisible (le token reste l'autorité). La vitrine (V2-23c)
    # surcharge par son propre chemin `/v/{token}` (jamais un `/g/…` qui ne
    # résoudrait pas).
    plain_name = prop.get("name") or _t(lang, "home")
    share_title = f"{plain_name} — {_t(lang, 'title_suffix')}"
    _path = canonical_path if canonical_path is not None else share_path(prop.get("name"), token)
    og_url = (base_url.rstrip("/") + _path) if base_url else ""
    og_html = _og_tags(title=share_title, desc=_t(lang, "share_desc"),
                       url=og_url, image=og_image_url,
                       locale=_OG_LOCALE.get(lang, "fr_FR"))

    # Bandeau « Aperçu du guide » (vitrine) : honnêteté vis-à-vis du prospect, et
    # le propriétaire voit d'un coup d'œil quel lien il a envoyé.
    showcase_banner = (f'<div class="showcase-banner">'
                       f'{_esc(_t(lang, "showcase_banner"))}</div>'
                       if variant == "showcase" else "")
    # Accueil personnalisé (lien de séjour).
    welcome = (_stay_welcome_html(stay, lang)
               if variant == "stay" and stay else "")
    # §3.5 (V2-23c) : sur un lien de séjour dont la fiche connaît la langue du
    # locataire, on l'expose au DOM (`data-guest-lang`, UNE seule source de vérité)
    # → `app.js` sait que « la fiche sait » et n'y superpose plus ni la préférence
    # mémorisée ni `navigator.language` (M-09 reste intact sur `/g/` et quand la
    # fiche ne sait rien). Le routeur ne pose `stay["guest_lang"]` que si la langue
    # est réellement offerte par ce guide (repli sinon — invariant 15).
    guest_lang_attr = (f' data-guest-lang="{_esc(stay["guest_lang"])}"'
                       if variant == "stay" and stay and stay.get("guest_lang")
                       else "")
    # Le manifeste PWA n'existe que pour le lien maison (`/g/`, QR imprimé, à
    # vie) : installer une PWA depuis un lien de séjour qui meurt à J+7 la
    # casserait (volet 1bis, §3). `manifest=False` sur séjour ET vitrine.
    manifest_link = (f'<link rel="manifest" href="/g/{_esc(token)}/manifest.webmanifest">'
                     if manifest else "")

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
{manifest_link}
<link rel="apple-touch-icon" href="/guide/icon-192.png">
<link rel="icon" href="/guide/icon-192.png">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,700&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{versioned('/guide/guide.css')}">
</head>
<body data-token="{_esc(token)}" data-api-base="{_esc(api_base)}" data-lang="{_esc(lang)}" data-default-lang="{_esc(default_lang)}"{guest_lang_attr} data-search-ph="{_esc(_t(lang, "search_placeholder"))}" data-search-none="{_esc(_t(lang, "search_none"))}" data-search-clear="{_esc(_t(lang, "search_clear"))}">
<div class="wrap">
  {showcase_banner}
  <header class="guide-head">
    <div class="hrow">
      <div>
        <div class="eyebrow">{_esc(_t(lang, "eyebrow"))}</div>
        <h1>{name}</h1>
        {f'<div class="city">{_esc(place)}</div>' if place else ''}
      </div>
      {langs}
    </div>
    {welcome}
    {sos}
  </header>
  {tabs_nav}
  <main id="content">{"".join(panels_html)}</main>
  <footer>{_esc(_t(lang, "footer"))}{_watermark_html(lang) if watermark else ''}</footer>
</div>
{back_float}
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
        "name": f"{prop.get('name') or 'Holaguia'} — Guide du logement",
        "short_name": (prop.get("name") or "Holaguia")[:24],
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
    # Cahier /s/ resté FR (M-13) : lang='fr' → overlay vide, rendu inchangé ; on
    # passe tout de même le code de section (cohérence des clés, sans effet ici).
    fields_html = _render_fields(schema, content, section_code=sec.get("code", ""))
    if fields_html:
        parts.append(fields_html)
    body_html = _md_to_html(sec.get("body_md"))
    if body_html:
        parts.append(f'<div class="prose">{body_html}</div>')
    parts.append(_render_media(sec.get("media") or []))
    body = "".join(p for p in parts if p)
    return f'<article class="sec-card staff-card">{body}</article>'


# ── Planning du cahier d'équipe (V2-23b, §2) ─────────────────────────────────
#
# L'entité affichée est la FENÊTRE, pas le séjour : *depuis quand la maison est
# libre*, *pour quand elle doit être prête*, *quoi faire*. Rendu server-side (le
# cahier reste lisible sans JS). Les données viennent de `care.build_planning`
# (pure) ; ici on ne fait que mettre en forme. Coordonnées déjà minimisées (RGPD)
# en amont : ce module ne « ré-ouvre » jamais un contact absent.

_WEEKDAYS_FR = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
_NATURE_LABELS_FR = {
    "reservation": "Réservation", "private": "Séjour privé", "works": "Travaux",
    "unavailable": "Indisponible", "unqualified": "À qualifier"}


def _fmt_wday_dm(d) -> str:
    """« sam. 01.08 » — jour de semaine + jour.mois (aucune surprise de fuseau)."""
    if not d:
        return ""
    return f"{_WEEKDAYS_FR[d.weekday()]} {d.day:02d}.{d.month:02d}"


def _fmt_hm(t) -> str:
    return t.strftime("%H:%M") if t else ""


def _fmt_window(minutes) -> str:
    """Durée de fenêtre lisible : « 5 h », « 3 h 30 », « 45 min »."""
    if minutes is None:
        return ""
    minutes = max(0, int(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m:02d}"
    if h:
        return f"{h} h"
    return f"{m} min"


def _render_prep_tasks(tasks: list[str]) -> str:
    if not tasks:
        return ""
    items = "".join(f"<li>{_esc(t)}</li>" for t in tasks)
    return f'<div class="prep-tasks"><span>À préparer</span><ul>{items}</ul></div>'


def _render_window_entry(e: dict) -> str:
    checkin = _fmt_hm(e.get("checkin_time"))
    luggage = _fmt_hm(e.get("luggage_drop_time"))
    free_days = e.get("free_days")
    lines: list[str] = []

    if e.get("same_day") and e.get("free_since_time") is not None:
        # Rotation même jour : libre au départ, prochaine arrivée le jour même.
        since = f"{_fmt_wday_dm(e['free_since_date'])} à {_fmt_hm(e['free_since_time'])}"
        win = _fmt_window(e.get("window_minutes"))
        lines.append(
            f"Libre depuis <b>{since}</b> · prochaine arrivée <b>{checkin}</b>"
            + (f" → <b>fenêtre {win}</b>" if win else ""))
    elif free_days and free_days >= 1:
        # Longue vacance : on ancre sur l'arrivée (rafraîchissement la veille),
        # sans urgence — la fenêtre en heures n'aurait pas de sens.
        d = "jour" if free_days == 1 else "jours"
        lines.append(
            f"Libre depuis <b>{free_days} {d}</b> "
            f"({_fmt_wday_dm(e.get('free_since_date'))}) · prochaine arrivée "
            f"<b>{_fmt_wday_dm(e['arrival_date'])} à {checkin}</b>")
    else:
        # Aucune occupation antérieure connue : on affiche l'arrivée seule.
        lines.append(f"Arrivée <b>{_fmt_wday_dm(e['arrival_date'])} à {checkin}</b>")

    if luggage:
        lines.append(
            f'<span class="prep-luggage">Dépôt de bagages annoncé à '
            f"<b>{luggage}</b> — accessible et présentable pour cette heure-là ; "
            f"entrée à {checkin}, tout fini.</span>")

    sig = e.get("signal") or {}
    alert = ""
    if sig.get("level") in ("amber", "red"):
        alert = (f'<div class="prep-alert prep-alert-{sig["level"]}">'
                 f'{_esc(sig.get("message") or "")}</div>')

    who = ""
    if e.get("guest_name"):
        who = f'<div class="prep-guest">{_esc(e["guest_name"])}</div>'

    body = "".join(f'<div class="prep-line">{ln}</div>' for ln in lines)
    return (f'<article class="prep-card prep-window prep-sig-{sig.get("level","na")}">'
            f'<div class="prep-when">{_esc(_fmt_wday_dm(e["arrival_date"]))}</div>'
            f'<div class="prep-main">{who}{body}{alert}'
            f'{_render_prep_tasks(e.get("tasks") or [])}</div></article>')


# Langue du locataire → libellé humain pour l'équipe (abord à l'arrivée, §3.0).
_GUEST_LANG_LABELS = {"fr": "français", "en": "anglais", "es": "espagnol",
                      "de": "allemand", "nl": "néerlandais", "it": "italien",
                      "pt": "portugais"}


def _render_guest_contact_links(e: dict) -> str:
    """Coordonnées cliquables du locataire pour une intervention en cours de
    séjour (§3.0) : le téléphone est une ACTION (appel + WhatsApp depuis le mobile
    de la personne qui fait le ménage), l'email en est une autre (mailto:). Déjà
    minimisées (RGPD) en amont — `care.build_planning` n'expose ces champs que pour
    les séjours en cours/à venir. Rien à afficher → chaîne vide."""
    phone = (e.get("guest_phone") or "").strip()
    email = (e.get("guest_email") or "").strip()
    links: list[str] = []
    if phone:
        links.append(f'<a class="prep-tel" href="tel:{_tel(phone)}">'
                     f'📞 {_esc(phone)}</a>')
        links.append(f'<a class="prep-wa" href="https://wa.me/{_tel(phone).lstrip("+")}" '
                     f'target="_blank" rel="noopener">WhatsApp</a>')
    if email:
        links.append(f'<a class="prep-mail" href="mailto:{_esc(email)}">'
                     f'✉︎ {_esc(email)}</a>')
    if not links:
        return ""
    return f'<div class="prep-contact">{" · ".join(links)}</div>'


def _render_midstay_entry(e: dict) -> str:
    who = _esc(e.get("guest_name") or "Locataire")
    lang = e.get("guest_lang")
    if lang:
        label = _GUEST_LANG_LABELS.get(lang, lang)
        who += f' · <span class="prep-lang">parle {_esc(label)}</span>'
    return (f'<article class="prep-card prep-midstay">'
            f'<div class="prep-when">{_esc(_fmt_wday_dm(e["on"]))}</div>'
            f'<div class="prep-main">'
            f'<div class="prep-line"><b>{_esc(e.get("label") or "Intervention")}</b>'
            f' — maison habitée, sur rendez-vous</div>'
            f'<div class="prep-guest">{who}</div>'
            f'{_render_guest_contact_links(e)}'
            f'{_render_prep_tasks(e.get("tasks") or [])}</div></article>')


def _render_idle_entry(e: dict) -> str:
    label = _NATURE_LABELS_FR.get(e.get("nature"), "Séjour")
    rng = f"{_fmt_wday_dm(e['on'])} → {_fmt_wday_dm(e.get('ends_on'))}"
    return (f'<article class="prep-card prep-idle">'
            f'<div class="prep-when">{_esc(_fmt_wday_dm(e["on"]))}</div>'
            f'<div class="prep-main">'
            f'<div class="prep-line"><b>{_esc(label)}</b> — rien à préparer</div>'
            f'<div class="muted prep-range">{_esc(rng)}</div></div></article>')


def _render_planning(planning: list[dict]) -> str:
    """Frise chronologique du cahier d'équipe (§2). État vide explicite : aucune
    préparation à venir n'est un message rassurant, pas un trou."""
    if not planning:
        return ('<section class="prep-block"><h2 class="prep-title">Préparations '
                'à venir</h2><div class="staff-empty prep-empty"><p>Aucune '
                "préparation à venir pour l'instant. Les fenêtres apparaîtront ici "
                "dès qu'un séjour sera programmé.</p></div></section>")
    rows = []
    for e in planning:
        if e["kind"] == "window":
            rows.append(_render_window_entry(e))
        elif e["kind"] == "midstay":
            rows.append(_render_midstay_entry(e))
        else:
            rows.append(_render_idle_entry(e))
    return ('<section class="prep-block"><h2 class="prep-title">Préparations à '
            'venir</h2><div class="prep-list">' + "".join(rows) + "</div></section>")


def render_staff(prop: dict, sections: list[dict], token: str,
                 planning: list[dict] | None = None) -> str:
    """Cahier de préparation mobile de l'équipe d'entretien (§M-13).

    Jamais indexé, jamais de secrets ni de POI. Reste lisible sans JS (rendu
    côté serveur). Affiche un état vide explicite si aucune consigne n'est
    encore saisie (le cahier peut être ouvert dès la création du logement)."""
    name = _esc(prop.get("name") or "Votre logement")
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)
    draft = prop.get("status") != "published"

    # Planning (§2) : la frise des fenêtres passe **en tête** — c'est ce dont
    # l'équipe a besoin en priorité (« quand, pour quand, quoi »). Les consignes
    # (sections) sont le « comment » et suivent. Absent si `planning is None`
    # (appel historique du rendu sans planning → rétrocompatibilité des tests).
    planning_html = _render_planning(planning) if planning is not None else ""

    if sections:
        instructions = "".join(_render_staff_section(s) for s in sections)
        instructions = (f'<section class="prep-block"><h2 class="prep-title">'
                        f'Consignes de préparation</h2>{instructions}</section>'
                        if planning is not None else instructions)
        body = planning_html + instructions
        # Retour en haut (V2-27) : sur une check-list longue, un chemin de retour
        # visible (page à défilement unique, sans onglets). Ancre native → #content.
        body += ('<a class="back-services" href="#content">'
                 f'{_esc(_UI["fr"]["back_top"])}</a>')
    elif planning is not None:
        body = planning_html + (
            '<div class="staff-empty"><p>Aucune consigne de préparation '
            "n'a encore été saisie pour ce logement. Revenez après que le "
            "propriétaire l'aura complété.</p></div>")
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
  <footer>Cahier interne Holaguia — réservé à l'équipe de préparation.</footer>
</div>
</body>
</html>"""


def render_staff_locked(prop: dict) -> str:
    """Cahier équipe indisponible : le plan du propriétaire n'inclut pas le guide
    équipe (réservé à l'offre Pro, V2-18b). Page sobre et digne — un membre de
    l'équipe peut tomber dessus : aucun message d'erreur brut, jamais de secret.
    Jamais indexé."""
    name = _esc(prop.get("name") or "Ce logement")
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#334049">
<title>Cahier équipe indisponible</title>
<link rel="stylesheet" href="{versioned('/guide/guide.css')}">
</head>
<body class="staff-page">
<div class="wrap notfound">
  <div class="nf-card">
    <div class="nf-emoji">🧹</div>
    <h1>Cahier de l'équipe indisponible</h1>
    <p>Le guide de l'équipe d'entretien de « {name} » est disponible avec
       l'offre&nbsp;Pro. Demandez à votre hôte d'activer l'offre Pro pour
       accéder au cahier de préparation.</p>
  </div>
</div>
</body>
</html>"""


def render_stay_expired() -> str:
    """Page neutre servie pour TOUS les cas morts des préfixes `/b/` (séjour) et
    `/v/` (vitrine) — token inconnu, séjour annulé, lien de séjour expiré (V2-23c,
    §1.2/§1.5). AUCUNE donnée du logement (pas même le nom) : un ancien locataire —
    ou un lien cassé — ne doit jamais retomber sur les vrais secrets. Jamais indexé.

    Libellé neutre volet 1bis (§4) : la même page sert un séjour expiré ET une
    vitrine inconnue (une vitrine n'« expire » pas, un prospect n'a pas d'« hôte »)
    → un ton unique, valable dans les deux cas, en FR/EN/ES."""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Lien non valable</title>
<link rel="stylesheet" href="{versioned('/guide/guide.css')}">
</head>
<body>
<div class="wrap notfound">
  <div class="nf-card">
    <div class="nf-emoji">⌛</div>
    <h1>Ce lien n'est plus valable</h1>
    <p>Demandez un lien à jour à la personne qui vous l'a envoyé.</p>
    <p lang="en">This link is no longer valid — please ask the person who sent it
       to you for an up-to-date link.</p>
    <p lang="es">Este enlace ya no es válido. Pídele un enlace actualizado a la
       persona que te lo envió.</p>
  </div>
</div>
</body>
</html>"""


def render_not_found() -> str:
    """Page 404 propre : token inconnu ou logement non publié (on ne révèle rien)."""
    return f"""<!DOCTYPE html>
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
