"""Registre public des langues (V2-21a, volet 1).

`GET /languages` expose au front les langues **publiées** du registre — la
source unique des langues offertes par le produit (invariant 8 étendu aux
langues). Une langue en brouillon ou en relecture n'apparaît jamais ici, donc
jamais dans les sélecteurs qui s'en nourrissent (menu de partage, modale
séjour). Le SSR du guide lit la base directement (il n'appelle pas cet endpoint).
"""
from fastapi import APIRouter

from .. import repo
from ..deps import Conn

router = APIRouter(tags=["languages"])


@router.get("/languages")
def list_languages(conn: Conn):
    """Langues offertes par le produit : uniquement les `published`, ordonnées.
    `published` est toujours `true` (par construction) — présent pour que le
    contrat soit explicite côté front."""
    return [{"code": l["code"], "name_native": l["name_native"], "published": True}
            for l in repo.published_languages(conn)]
