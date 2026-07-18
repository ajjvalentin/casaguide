"""Endpoints publics du guide voyageur (§3.2, M-08).

    GET /g/{guide_token}          → page HTML voyageur (mobile-first, PWA)
    GET /g/{guide_token}/data     → guide JSON pré-calculé (pour l'app, M-09)
    GET /g/{guide_token}/secrets  → wifi / boîte à clés (mode d'accès 'link')
    GET /g/{guide_token}/media/{id} → fichier média d'une section visible
    GET /guide/sw.js              → service worker (portée '/' pour le hors-ligne)

Tout est pré-calculé en base : aucun appel API externe (invariant 4). Réponses
`noindex` (§8, token secret ≥ 128 bits). Les JSON publics déclarent explicitement
`charset=utf-8` (mojibake constaté dans Safari sur le JSON sans charset).

Les secrets (wifi, code boîte à clés) ne transitent **jamais** par la page HTML
ni par `/data` : déchiffrement à la demande sur `/secrets`, réservé au mode
d'accès 'link' du MVP (le lien secret tient lieu de clé d'accès, §8).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from .. import (assets, crypto, guide_page, media_files, og_image, repo,
                storage, wifi)
from ..config import settings
from ..deps import Conn

router = APIRouter(tags=["guide"])

# backend/api/routers/guide.py → parents[3] = racine du dépôt → /frontend
_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

# Cache court (le contenu ne change qu'à (re)publication)
_NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}


def _public_headers(no_store: bool = False) -> dict[str, str]:
    cache = "no-store" if no_store else f"public, max-age={settings.guide_cache_seconds}"
    return {**_NOINDEX, "Cache-Control": cache}


def _json(payload: dict, *, no_store: bool = False) -> JSONResponse:
    """JSON public avec charset explicite (évite le mojibake Safari)."""
    return JSONResponse(
        content=jsonable_encoder(payload),
        media_type="application/json; charset=utf-8",
        headers=_public_headers(no_store=no_store),
    )


def _real_token(token_or_slug: str) -> str:
    """Extrait le token réel d'un lien de partage `/g/{slug}-{token}` (M-25).

    Le token est purement hexadécimal (`gen_random_bytes`) et le slug décoratif
    précède le dernier tiret : le token est donc le segment après le dernier `-`.
    Un ancien lien nu `/g/{token}` (sans tiret) est renvoyé tel quel."""
    return token_or_slug.rsplit("-", 1)[-1]


def _base_url(request: Request) -> str:
    """Origine publique des liens absolus (M-25) : `CASAGUIDE_PUBLIC_BASE_URL`
    sinon l'origine de la requête."""
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _first_photo_path(sections: list[dict], property_media: list[dict],
                      token: str) -> str | None:
    """Chemin de la première photo du logement (M-25) : photos de niveau
    logement d'abord (« façade »), puis premières photos des sections visibles,
    dans l'ordre du guide. `None` si le logement n'a aucune photo."""
    for m in property_media:
        if m.get("kind") == "photo":
            return m["url"]
    for s in sections:
        for m in s.get("media") or []:
            if m.get("kind") == "photo":
                return m["url"]
    return None


def _effective_lang(prop: dict, requested: str | None) -> str:
    """Langue de rendu : `requested` seulement si c'est une langue publiée et
    non la langue source ; sinon la langue source (repli, jamais de trou, §9)."""
    default = prop.get("default_lang") or "fr"
    if requested and requested != default and requested in (prop.get("published_langs") or []):
        return requested
    return default


def _load_guide(conn, token: str, lang: str | None = None):
    """Charge un guide publié : (prop, sections, pois, area_facts, media, lang).
    404 sinon.

    Les médias des sections **visibles** (et ceux du logement) sont rattachés à
    leur section ; chacun porte l'URL de son endpoint public. Un média de section
    masquée n'est jamais listé (invariant de visibilité, M-12).

    Si une langue traduite est demandée (M-09), le contenu **textuel** des
    sections et des POI est overlayé depuis les traductions stockées ; tout
    segment non traduit retombe sur le français (repli élégant, §9)."""
    prop = repo.get_published_property_by_token(conn, token)
    if not prop:
        return None
    pid = str(prop["id"])
    sections = repo.guide_sections(conn, pid)
    pois = repo.guide_pois(conn, pid)
    area_facts = repo.guide_area_facts(conn, prop["country_code"], prop["city"])

    effective = _effective_lang(prop, lang)
    if effective != (prop.get("default_lang") or "fr"):
        _overlay_translations(conn, pid, effective, sections, pois)

    media_by_section: dict[str, list] = {}
    property_media: list = []
    for m in repo.guide_media(conn, pid):
        item = {"id": str(m["id"]), "kind": m["kind"], "caption": m["caption"],
                "sort_order": m["sort_order"], "url": f"/g/{token}/media/{m['id']}"}
        if m["section_code"]:
            media_by_section.setdefault(m["section_code"], []).append(item)
        else:
            property_media.append(item)
    for s in sections:
        s["media"] = media_by_section.get(s["code"], [])
    return prop, sections, pois, area_facts, property_media, effective


def _overlay_translations(conn, pid: str, lang: str, sections: list[dict],
                          pois: list[dict]) -> None:
    """Remplace en place le contenu textuel source par sa traduction `lang`.
    Les champs structurés et les segments non traduits restent inchangés."""
    sec_tr = repo.guide_section_translations(conn, pid, lang)
    for s in sections:
        tr = sec_tr.get(s["code"])
        if tr:
            if tr["content"] is not None:
                s["content"] = tr["content"]
            if tr["body_md"]:
                s["body_md"] = tr["body_md"]
    poi_tr = repo.guide_poi_translations(conn, pid, lang)
    for p in pois:
        tr = poi_tr.get(str(p["id"]))
        if tr:
            if tr["description_md"]:
                p["description_md"] = tr["description_md"]
            if tr["owner_comment"]:
                p["owner_comment"] = tr["owner_comment"]


@router.get("/g/{guide_token}", response_class=HTMLResponse)
def public_guide_page(guide_token: str, conn: Conn, request: Request,
                      lang: str | None = None):
    """Page HTML du guide voyageur, rendue dans `lang` si c'est une langue
    publiée (M-09 ; repli sur la langue source sinon). 404 propre si token
    inconnu / non publié. Accepte le lien de partage `/g/{slug}-{token}` (M-25) :
    le slug est décoratif, seul le token fait foi."""
    token = _real_token(guide_token)
    loaded = _load_guide(conn, token, lang)
    if not loaded:
        return HTMLResponse(guide_page.render_not_found(), status_code=404,
                            headers=_NOINDEX)
    prop, sections, pois, area_facts, property_media, effective = loaded
    # Vignette de partage (M-25) : première photo du logement, sinon image de
    # marque générée. URL absolue pour les scrapers (WhatsApp/iMessage/e-mail).
    base = _base_url(request)
    photo = _first_photo_path(sections, property_media, token)
    og_image_url = base + (photo or f"/g/{token}/og-image.png")
    html = guide_page.render_guide(_property_public(prop), sections, pois,
                                   area_facts, token, lang=effective,
                                   base_url=base, og_image_url=og_image_url)
    return HTMLResponse(html, headers=_public_headers())


@router.get("/g/{guide_token}/og-image.png")
def public_og_image(guide_token: str, conn: Conn):
    """Image de marque 1200×630 pour les liens de partage (M-25), servie quand
    le logement n'a aucune photo. 404 propre si le guide n'est pas publié."""
    token = _real_token(guide_token)
    prop = repo.get_published_property_by_token(conn, token)
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Guide introuvable")
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)
    png = og_image.build_og_image(prop["name"], subtitle=place)
    return Response(content=png, media_type="image/png",
                    headers=_public_headers())


@router.get("/g/{guide_token}/data")
def public_guide_data(guide_token: str, conn: Conn, lang: str | None = None):
    """Guide JSON pré-calculé (sans aucun secret) pour l'app / usages tiers.
    Le contenu est renvoyé dans `lang` si c'est une langue publiée (M-09).
    Accepte aussi le lien de partage `/g/{slug}-{token}/data` (M-25)."""
    loaded = _load_guide(conn, _real_token(guide_token), lang)
    if not loaded:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Guide introuvable")
    prop, sections, pois, area_facts, property_media, effective = loaded
    return _json({
        "property": _property_public(prop),
        "lang": effective,
        "sections": sections,
        "pois": pois,
        "area_facts": area_facts,
        "media": property_media,
    })


@router.get("/g/{guide_token}/secrets")
def public_guide_secrets(guide_token: str, conn: Conn):
    """Wifi et code boîte à clés d'un guide publié en mode 'link' (MVP, §8).

    Déchiffrement à la demande — jamais dans la page HTML ni dans `/data`. Renvoie
    un objet vide (jamais 404) si aucun secret, chiffrement non configuré, ou mode
    d'accès non 'link' : le client masque simplement les blocs correspondants."""
    empty = {"wifi_networks": [], "wifi_ssid": None, "wifi_pass": None,
             "keybox_code": None, "keybox_notes": None}
    if not crypto.is_configured():
        return _json(empty, no_store=True)
    row = repo.get_published_secrets_by_token(conn, guide_token)
    if not row:
        return _json(empty, no_store=True)
    # Multi-wifi (M-15) : liste déchiffrée + repli legacy sur le réseau n°1. Les
    # anciens champs restent alimentés depuis le réseau n°1 (rétrocompat app.js).
    networks = wifi.networks_from_row(row)
    net1 = wifi.first_network(networks)
    return _json({
        "wifi_networks": networks,
        "wifi_ssid": net1["ssid"] if net1 else None,
        "wifi_pass": net1["pass"] if net1 else None,
        "keybox_code": crypto.decrypt(row["keybox_code_enc"]),
        "keybox_notes": row["keybox_notes"],
    }, no_store=True)


@router.get("/g/{guide_token}/manifest.webmanifest")
def public_manifest(guide_token: str, conn: Conn):
    """Manifest PWA propre au guide (start_url/scope = ce guide). 404 si non publié."""
    prop = repo.get_published_property_by_token(conn, guide_token)
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Guide introuvable")
    return JSONResponse(
        content=guide_page.build_manifest(_property_public(prop), guide_token),
        media_type="application/manifest+json; charset=utf-8",
        headers=_public_headers(),
    )


@router.get("/g/{guide_token}/media/{media_id}")
def public_media(guide_token: str, media_id: str, conn: Conn):
    """Sert un fichier média d'un guide publié — uniquement si sa section est
    visible (ou s'il est rattaché au logement). 404 sinon, sans rien révéler."""
    row = repo.get_public_media(conn, guide_token, media_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Média introuvable")
    try:
        data = storage.get_storage().read(row["storage_key"])
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Fichier introuvable")
    return Response(
        content=data,
        media_type=media_files.content_type_for_key(row["storage_key"]),
        headers=_public_headers(),
    )


@router.get("/guide/sw.js")
def service_worker():
    """Service worker du guide, servi avec `Service-Worker-Allowed: /` pour lui
    accorder une portée couvrant `/g/…` (la page) et `/guide/…` (l'app shell),
    tout en le laissant physiquement sous `/guide/`. Sans cet entête, sa portée
    serait limitée à `/guide/` et n'intercepterait pas les navigations `/g/…`."""
    path = _FRONTEND_DIR / "guide" / "sw.js"
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # M-11 : injecter le SHA du déploiement dans le nom des caches du SW → chaque
    # déploiement le réactive et purge les anciens caches (cache-busting auto).
    body = path.read_text(encoding="utf-8").replace(
        assets.ASSET_VERSION_PLACEHOLDER, assets.asset_version())
    return Response(
        content=body,
        media_type="application/javascript; charset=utf-8",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# ── Cahier de préparation « équipe d'entretien » (/s/{staff_token}, M-13) ─────
# Second espace public, distinct du guide voyageur : il n'expose QUE les sections
# audience='staff' (jamais les secrets, jamais les POI/carte, jamais les sections
# 'guest' — invariant 7). Accessible **même en brouillon** : l'équipe prépare le
# logement AVANT sa mise en ligne (justifie l'absence de filtre sur `status`).
# 404 propre si le staff_token est inconnu.

def _load_staff(conn, token: str):
    """Charge un cahier 'staff' par son token : (prop, sections). 404 sinon.
    Les médias des sections 'staff' visibles sont rattachés à leur section."""
    prop = repo.get_property_by_staff_token(conn, token)
    if not prop:
        return None
    pid = str(prop["id"])
    sections = repo.staff_sections(conn, pid)
    media_by_section: dict[str, list] = {}
    for m in repo.staff_media(conn, pid):
        media_by_section.setdefault(m["section_code"], []).append(
            {"id": str(m["id"]), "kind": m["kind"], "caption": m["caption"],
             "sort_order": m["sort_order"], "url": f"/s/{token}/media/{m['id']}"})
    for s in sections:
        s["media"] = media_by_section.get(s["code"], [])
    return prop, sections


@router.get("/s/{staff_token}", response_class=HTMLResponse)
def staff_cahier_page(staff_token: str, conn: Conn):
    """Page HTML du cahier de préparation. 404 propre si le token est inconnu.
    Servie même en brouillon (l'équipe prépare avant publication) ; jamais mise
    en cache partagé (`no-store`) car le contenu évolue pendant la préparation."""
    loaded = _load_staff(conn, staff_token)
    if not loaded:
        return HTMLResponse(guide_page.render_not_found(), status_code=404,
                            headers=_NOINDEX)
    prop, sections = loaded
    html = guide_page.render_staff(prop, sections, staff_token)
    return HTMLResponse(html, headers=_public_headers(no_store=True))


@router.get("/s/{staff_token}/media/{media_id}")
def staff_media_file(staff_token: str, media_id: str, conn: Conn):
    """Sert un média d'un cahier 'staff' — uniquement une section 'staff' visible.
    404 sinon, sans rien révéler (jamais un média 'guest' ni de section masquée)."""
    row = repo.get_staff_media(conn, staff_token, media_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Média introuvable")
    try:
        data = storage.get_storage().read(row["storage_key"])
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Fichier introuvable")
    return Response(
        content=data,
        media_type=media_files.content_type_for_key(row["storage_key"]),
        headers=_public_headers(no_store=True),
    )


def _property_public(prop: dict) -> dict:
    """Vue publique du logement (jamais de secrets — invariant 5)."""
    return {
        "name": prop["name"],
        "address_line1": prop["address_line1"],
        "address_line2": prop["address_line2"],
        "postal_code": prop["postal_code"],
        "city": prop["city"],
        "region": prop["region"],
        "country_code": prop["country_code"],
        "lat": prop["lat"],
        "lon": prop["lon"],
        "default_lang": prop["default_lang"],
        "published_langs": prop["published_langs"],
        "tourism_license": prop["tourism_license"],
        "contact": {
            "name": prop["contact_name"],
            "phone": prop["contact_phone"],
            "whatsapp": prop["contact_whatsapp"],
            "email": prop["contact_email"],
            "backup": prop["contact_backup"],
        },
    }
