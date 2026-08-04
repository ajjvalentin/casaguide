"""Registre public des langues (V2-21a, volet 1).

`GET /languages` expose au front les langues **publiées** du registre — la
source unique des langues offertes par le produit (invariant 8 étendu aux
langues). Une langue en brouillon ou en relecture n'apparaît jamais ici, donc
jamais dans les sélecteurs qui s'en nourrissent (menu de partage, modale
séjour). Le SSR du guide lit la base directement (il n'appelle pas cet endpoint).
"""
from fastapi import APIRouter

from .. import emails, guide_page, i18n, repo
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

# Gabarits mailto/wa.me PAR CIBLE (V2-23d, volet 1, §3) : le texte brut partage les
# MÊMES copies que l'email HTML backend (`emails._EMAIL`, clés `email.*`) — une
# seule source par cible. La MAISON (`kind='house'`) garde le gabarit utilitaire
# générique `ui.send_*` (lien réutilisable du QR, pas d'email backend). Le champ
# `signoff` n'existe que pour la vitrine.
_EMAIL_TPL_BY_KIND = {
    "stay": {"subject": "stay_subject", "hello": "stay_hello",
             "hello_generic": "stay_hello_generic", "intro": "stay_intro",
             "signoff": None},
    "showcase": {"subject": "showcase_subject", "hello": "showcase_hello",
                 "hello_generic": "showcase_hello", "intro": "showcase_intro",
                 "signoff": "showcase_signoff"},
}


@router.get("/send-templates", response_model=SendTemplatesOut)
def send_templates(conn: Conn, lang: str = "fr", kind: str = "house"):
    """Gabarits courts d'envoi du guide (mailto/wa.me) résolus dans `lang` et selon
    la CIBLE (`kind` : stay | showcase | house) : overlay des langues publiées
    supplémentaires d'abord (`ui_translations`), puis le code FR/EN/ES, puis le FR
    (repli, jamais de trou — même mécanique que le SSR). Séjour/vitrine partagent
    les copies de l'email HTML backend (`email.*`) ; la maison garde le gabarit
    générique (`ui.send_*`). La fenêtre substitue `{property}`/`{name}`/`{start}`/
    `{end}` et l'URL côté client."""
    tok = i18n.set_overlay(repo.ui_translations(conn, lang))
    try:
        tpl = _EMAIL_TPL_BY_KIND.get(kind)
        if tpl:
            def val(field: str) -> str:
                key = tpl[field]
                return emails._et(lang, key) if key else ""
            return SendTemplatesOut(
                subject=val("subject"), hello=val("hello"),
                hello_generic=val("hello_generic"), intro=val("intro"),
                signoff=val("signoff"))
        t = {k: guide_page._t(lang, k) for k in _SEND_KEYS}
    finally:
        i18n.reset_overlay(tok)
    return SendTemplatesOut(subject=t["send_subject"], hello=t["send_hello"],
                            hello_generic=t["send_hello_generic"],
                            intro=t["send_intro"], signoff=t["send_signoff"])
