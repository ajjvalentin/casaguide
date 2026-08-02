"""Registre public des langues (V2-21a, volet 1).

`GET /languages` expose au front les langues **publiées** du registre — la
source unique des langues offertes par le produit (invariant 8 étendu aux
langues). Une langue en brouillon ou en relecture n'apparaît jamais ici, donc
jamais dans les sélecteurs qui s'en nourrissent (menu de partage, modale
séjour). Le SSR du guide lit la base directement (il n'appelle pas cet endpoint).
"""
from fastapi import APIRouter

from .. import guide_page, i18n, repo
from ..deps import Conn
from ..schemas import SendTemplatesOut

router = APIRouter(tags=["languages"])


@router.get("/languages")
def list_languages(conn: Conn):
    """Langues offertes par le produit : uniquement les `published`, ordonnées.
    `published` est toujours `true` (par construction) — présent pour que le
    contrat soit explicite côté front."""
    return [{"code": l["code"], "name_native": l["name_native"], "published": True}
            for l in repo.published_languages(conn)]


# Clés d'inventaire des gabarits d'envoi (V2-23c, volet 3) — portées par
# `guide_page._UI` (FR/EN/ES) et l'overlay `ui_translations` (langues
# supplémentaires). Une seule source de vérité : la fenêtre d'envoi ne code aucun
# libellé, elle les lit ici (localisés) et compose le message côté client.
_SEND_KEYS = ("send_subject", "send_hello", "send_hello_generic",
              "send_intro", "send_signoff")


@router.get("/send-templates", response_model=SendTemplatesOut)
def send_templates(conn: Conn, lang: str = "fr"):
    """Gabarits courts d'envoi du guide (email/WhatsApp) résolus dans `lang` :
    overlay des langues publiées supplémentaires d'abord (`ui_translations`), puis
    le code FR/EN/ES, puis le FR (repli, jamais de trou — même mécanique que le
    rendu SSR du guide). La fenêtre d'envoi substitue `{property}`/`{name}` et
    l'URL côté client."""
    tok = i18n.set_overlay(repo.ui_translations(conn, lang))
    try:
        t = {k: guide_page._t(lang, k) for k in _SEND_KEYS}
    finally:
        i18n.reset_overlay(tok)
    return SendTemplatesOut(subject=t["send_subject"], hello=t["send_hello"],
                            hello_generic=t["send_hello_generic"],
                            intro=t["send_intro"], signoff=t["send_signoff"])
