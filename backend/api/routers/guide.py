"""Endpoint public du guide voyageur (§3.2, §12 étape 1).

    GET /g/{guide_token}

Sert un guide entièrement pré-calculé en base :
  * infos publiques du logement (jamais les secrets — invariant 5) ;
  * sections visibles avec leur contenu ;
  * POI approuvés / édités uniquement (jamais 'suggested' ni 'rejected') ;
  * faits locaux (urgences, tri, bruit) mutualisés par zone.

Aucun appel API externe (invariant 4) : tout provient de PostgreSQL. Réponse
marquée `noindex` (§8, token secret ≥ 128 bits) et mise en cache courte.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from .. import media_files, repo, storage
from ..config import settings
from ..deps import Conn

router = APIRouter(tags=["guide"])


@router.get("/g/{guide_token}")
def public_guide(guide_token: str, conn: Conn, response: Response):
    prop = repo.get_published_property_by_token(conn, guide_token)
    if not prop:
        # Ne révèle pas si le token est inconnu ou le guide non publié
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Guide introuvable")

    pid = str(prop["id"])
    sections = repo.guide_sections(conn, pid)
    pois = repo.guide_pois(conn, pid)
    area_facts = repo.guide_area_facts(conn, prop["country_code"], prop["city"])

    # Médias (M-12) : seulement ceux des sections visibles + ceux du logement.
    # Chaque média porte l'URL de son endpoint public (re-vérifié à la lecture).
    media_by_section: dict[str, list] = {}
    property_media: list = []
    for m in repo.guide_media(conn, pid):
        item = {"id": str(m["id"]), "kind": m["kind"], "caption": m["caption"],
                "sort_order": m["sort_order"],
                "url": f"/g/{guide_token}/media/{m['id']}"}
        if m["section_code"]:
            media_by_section.setdefault(m["section_code"], []).append(item)
        else:
            property_media.append(item)
    for s in sections:
        s["media"] = media_by_section.get(s["code"], [])

    # Entêtes : pas d'indexation, cache court (le contenu ne change qu'à publication)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = f"public, max-age={settings.guide_cache_seconds}"

    return {
        "property": {
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
        },
        "sections": sections,
        "pois": pois,
        "area_facts": area_facts,
        "media": property_media,
    }


@router.get("/g/{guide_token}/media/{media_id}")
def public_media(guide_token: str, media_id: str, conn: Conn, response: Response):
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
        headers={"X-Robots-Tag": "noindex, nofollow",
                 "Cache-Control": f"public, max-age={settings.guide_cache_seconds}"},
    )
