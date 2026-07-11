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

from .. import repo
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
    }
