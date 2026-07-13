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

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from .. import crypto, guide_page, media_files, repo, storage
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


def _load_guide(conn, token: str):
    """Charge un guide publié : (prop, sections, pois, area_facts). 404 sinon.

    Les médias des sections **visibles** (et ceux du logement) sont rattachés à
    leur section ; chacun porte l'URL de son endpoint public. Un média de section
    masquée n'est jamais listé (invariant de visibilité, M-12)."""
    prop = repo.get_published_property_by_token(conn, token)
    if not prop:
        return None
    pid = str(prop["id"])
    sections = repo.guide_sections(conn, pid)
    pois = repo.guide_pois(conn, pid)
    area_facts = repo.guide_area_facts(conn, prop["country_code"], prop["city"])

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
    return prop, sections, pois, area_facts, property_media


@router.get("/g/{guide_token}", response_class=HTMLResponse)
def public_guide_page(guide_token: str, conn: Conn):
    """Page HTML du guide voyageur. 404 propre si token inconnu / non publié."""
    loaded = _load_guide(conn, guide_token)
    if not loaded:
        return HTMLResponse(guide_page.render_not_found(), status_code=404,
                            headers=_NOINDEX)
    prop, sections, pois, area_facts, _property_media = loaded
    html = guide_page.render_guide(_property_public(prop), sections, pois,
                                   area_facts, guide_token)
    return HTMLResponse(html, headers=_public_headers())


@router.get("/g/{guide_token}/data")
def public_guide_data(guide_token: str, conn: Conn):
    """Guide JSON pré-calculé (sans aucun secret) pour l'app / usages tiers."""
    loaded = _load_guide(conn, guide_token)
    if not loaded:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Guide introuvable")
    prop, sections, pois, area_facts, property_media = loaded
    return _json({
        "property": _property_public(prop),
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
    empty = {"wifi_ssid": None, "wifi_pass": None, "keybox_code": None,
             "keybox_notes": None}
    if not crypto.is_configured():
        return _json(empty, no_store=True)
    row = repo.get_published_secrets_by_token(conn, guide_token)
    if not row:
        return _json(empty, no_store=True)
    return _json({
        "wifi_ssid": row["wifi_ssid"],
        "wifi_pass": crypto.decrypt(row["wifi_pass_enc"]),
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
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


def _property_public(prop: dict) -> dict:
    """Vue publique du logement (jamais de secrets — invariant 5)."""
    return {
        "name": prop["name"],
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
