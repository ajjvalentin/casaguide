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

import datetime as _dt
import logging
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, HTTPException, Request,
                     Response, status)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from .. import (assets, care, crypto, emails, guide_page, media_files, og_image,
                plans, repo, storage, wifi)
from ..config import settings
from ..deps import Conn, Mailer
from ..schemas import GuestServiceRequestIn, GuestServiceRequestOut

log = logging.getLogger("casaguide.guide")

router = APIRouter(tags=["guide"])

# backend/api/routers/guide.py → parents[3] = racine du dépôt → /frontend
_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

# Cache court (le contenu ne change qu'à (re)publication)
_NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}

# Lien de séjour (V2-23c, §1.3) : expire J+7 après le départ — un ancien locataire
# ne garde pas l'accès au code de la boîte à clés. Le seuil vit ici, jamais
# éparpillé en littéral.
STAY_EXPIRY_DAYS = 7


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


def _guide_langs(conn, prop: dict) -> list[str]:
    """Langues effectivement offertes sur CE guide : langue source + langues
    publiées du logement, mais bornées au REGISTRE (V2-21a) — une langue passée
    en 'draft'/'in_review' disparaît du guide même si elle reste dans
    `published_langs` du logement. Le registre fait foi (invariant 8 étendu)."""
    registry = repo.published_language_codes(conn)
    default = prop.get("default_lang") or "fr"
    prop_langs = [l for l in (prop.get("published_langs") or []) if l in registry]
    return [default] + [l for l in prop_langs if l != default]


def _effective_lang(conn, prop: dict, requested: str | None) -> str:
    """Langue de rendu : `requested` seulement si c'est une langue offerte sur ce
    guide (publiée pour le logement ET dans le registre) et non la langue source ;
    sinon la langue source (repli, jamais de trou, §9)."""
    default = prop.get("default_lang") or "fr"
    if requested and requested != default and requested in _guide_langs(conn, prop):
        return requested
    return default


def _assemble_guide(conn, prop: dict, lang: str | None, *, media_base: str):
    """Assemble le contenu d'un guide publié à partir de son logement `prop` :
    (prop, sections, pois, area_facts, media, lang effective, noms de langues).

    Les médias des sections **visibles** (et ceux du logement) sont rattachés à
    leur section ; chacun porte l'URL de son endpoint public (`media_base`, ex.
    `/g/{token}` pour le lien maison/séjour, `/v/{token}` pour la vitrine). Un
    média de section masquée n'est jamais listé (invariant de visibilité, M-12).

    Si une langue traduite est demandée (M-09), le contenu **textuel** des
    sections et des POI est overlayé depuis les traductions stockées ; tout
    segment non traduit retombe sur le français (repli élégant, §9)."""
    pid = str(prop["id"])
    sections = repo.guide_sections(conn, pid)
    pois = repo.guide_pois(conn, pid)
    area_facts = repo.guide_area_facts(conn, prop["country_code"], prop["city"])

    # Registre des langues (V2-21a) : carte code→nom natif des langues PUBLIÉES,
    # source unique du sélecteur et du filtrage. Chargée une fois ici.
    lang_names = {l["code"]: l["name_native"] for l in repo.published_languages(conn)}
    effective = _effective_lang(conn, prop, lang)
    if effective != (prop.get("default_lang") or "fr"):
        _overlay_translations(conn, pid, effective, sections, pois)

    media_by_section: dict[str, list] = {}
    property_media: list = []
    for m in repo.guide_media(conn, pid):
        item = {"id": str(m["id"]), "kind": m["kind"], "caption": m["caption"],
                "sort_order": m["sort_order"], "url": f"{media_base}/media/{m['id']}"}
        if m["section_code"]:
            media_by_section.setdefault(m["section_code"], []).append(item)
        else:
            property_media.append(item)
    for s in sections:
        s["media"] = media_by_section.get(s["code"], [])
    return prop, sections, pois, area_facts, property_media, effective, lang_names


def _load_guide(conn, token: str, lang: str | None = None):
    """Charge un guide publié par son `guide_token` (lien maison/séjour). None si
    token inconnu / non publié. Voir `_assemble_guide`."""
    prop = repo.get_published_property_by_token(conn, token)
    if not prop:
        return None
    return _assemble_guide(conn, prop, lang, media_base=f"/g/{token}")


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


def _stay_expired(booking: dict, today: _dt.date) -> bool:
    """Le lien de séjour expire J+7 après le départ (V2-23c, §1.3)."""
    return today > booking["ends_on"] + _dt.timedelta(days=STAY_EXPIRY_DAYS)


def _resolve_stay(conn, stay_token: str) -> dict | None:
    """Résout un `stay_token` en séjour valable (V2-23c). None dans les 3 cas
    morts : token inconnu, séjour annulé, ou lien expiré (J+8). Le router sert
    alors la page neutre (jamais le guide générique en silence : un lien de séjour
    cassé ne retombe pas sur les vrais secrets).

    Amendé 02/08 (préfixe dédié `/b/`) : plus de contrôle « même logement » — le
    `stay_token` désigne à lui seul le séjour ET son logement (résolu côté serveur
    par le router), il ne voyage plus accolé à un `guide_token` dans l'URL."""
    booking = repo.get_booking_by_stay_token(conn, stay_token)
    if (not booking
            or booking["status"] != "active"
            or _stay_expired(booking, _dt.date.today())):
        return None
    return booking


@router.get("/g/{guide_token}", response_class=HTMLResponse)
def public_guide_page(guide_token: str, conn: Conn, request: Request,
                      lang: str | None = None):
    """Page HTML du guide voyageur — **lien maison** (QR imprimé, à vie), rendue
    dans `lang` si c'est une langue publiée (M-09 ; repli sur la langue source
    sinon). 404 propre si token inconnu / non publié. Accepte le lien de partage
    `/g/{slug}-{token}` (M-25) : le slug est décoratif, seul le token fait foi.

    Le lien de SÉJOUR a son propre préfixe `/b/{stay_token}` (V2-23c) : le
    `guide_token` éternel ne voyage plus jamais dans l'URL envoyée au locataire."""
    token = _real_token(guide_token)
    prop_row = repo.get_published_property_by_token(conn, token)
    if not prop_row:
        return HTMLResponse(guide_page.render_not_found(), status_code=404,
                            headers=_NOINDEX)
    html = _render_guide_html(conn, prop_row, token, request, lang,
                              variant="house", stay_ctx=None)
    return HTMLResponse(html, headers=_public_headers())


def _render_guide_html(conn, prop_row: dict, token: str, request: Request,
                       lang: str | None, *, variant: str,
                       stay_ctx: dict | None) -> str:
    """Rendu commun aux liens maison (`/g/`) et séjour (`/b/`) : le lien de séjour
    n'est qu'un rendu différent du MÊME guide (accueil personnalisé, `guest_lang`
    par défaut), servi via le `guide_token` résolu côté serveur — l'app charge
    secrets/data/média sur `/g/{token}/…` comme d'habitude."""
    _p, sections, pois, area_facts, property_media, effective, lang_names = \
        _assemble_guide(conn, prop_row, lang, media_base=f"/g/{token}")
    # Vignette de partage (M-25) : première photo du logement, sinon image de
    # marque générée. URL absolue pour les scrapers (WhatsApp/iMessage/e-mail).
    base = _base_url(request)
    photo = _first_photo_path(sections, property_media, token)
    og_image_url = base + (photo or f"/g/{token}/og-image.png")
    # Marque blanche (V2-05a) : le plan gratuit affiche un pied de page discret
    # « Créé avec Holaguia » ; les plans payants ne l'ont pas (features.watermark).
    plan = repo.get_plan_by_guide_token(conn, token)
    watermark = plans.wants_watermark(plan) if plan else True
    # Libellés statiques traduits (V2-21a, volet 2) : superposés au rendu pour une
    # langue publiée supplémentaire ; vide pour FR/EN/ES (rendu identique).
    ui_overlay = repo.ui_translations(conn, effective)
    return guide_page.render_guide(_property_public(prop_row), sections, pois,
                                   area_facts, token, lang=effective,
                                   base_url=base, og_image_url=og_image_url,
                                   watermark=watermark, lang_names=lang_names,
                                   ui_overlay=ui_overlay, variant=variant,
                                   stay=stay_ctx)


# ── Lien de SÉJOUR (V2-23c, §1.2/§1.3, amendé 02/08 — préfixe dédié `/b/`) ─────
# `/b/{stay_token}` : le logement est résolu CÔTÉ SERVEUR depuis le séjour du
# token → le `guide_token` éternel (lien maison) ne voyage JAMAIS dans l'URL
# envoyée au locataire (correctif de sécurité : sur l'ancienne forme
# `/g/{guide_token}?s=…`, retirer `?s=` après J+7 suffisait à retrouver les vrais
# codes). `_real_token()` ne s'applique PAS à `/b/` : un lien de séjour est
# **envoyé, jamais retapé** → jamais de slug décoratif ; un `/b/{slug}-{token}`
# n'est donc pas rattrapé (page neutre — décision explicite, couverte par test).

@router.get("/b/{stay_token}", response_class=HTMLResponse)
def public_stay_page(stay_token: str, conn: Conn, request: Request,
                     lang: str | None = None):
    """Page HTML du guide en variant « séjour » : accueil personnalisé (nom +
    dates du séjour), langue par défaut = `guest_lang` du locataire (un `?lang=`
    explicite garde la priorité, §1.3), demandes rattachées au séjour du token.

    Tout cas mort (token inconnu, séjour annulé, J+8 après le départ, logement non
    publié) → la MÊME page neutre que la vitrine — jamais le guide générique, qui
    exposerait les vrais secrets à un ancien locataire."""
    stay = _resolve_stay(conn, stay_token)
    prop_row = (repo.get_published_property_by_id(conn, str(stay["property_id"]))
                if stay else None)
    if not prop_row:
        return HTMLResponse(guide_page.render_stay_expired(), status_code=404,
                            headers=_NOINDEX)
    token = prop_row["guide_token"]
    req_lang = lang or stay.get("guest_lang")
    stay_ctx = {"guest_name": stay.get("guest_name"),
                "starts_on": stay["starts_on"], "ends_on": stay["ends_on"],
                "stay_token": stay_token}
    html = _render_guide_html(conn, prop_row, token, request, req_lang,
                              variant="stay", stay_ctx=stay_ctx)
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


# ── Lien VITRINE (V2-23c, §1.4) ──────────────────────────────────────────────
# Préfixe distinct `/v/…` : un token vitrine ne peut jamais être confondu avec un
# guide réel (ni côté code, ni dans les journaux). Même gabarit que le guide (but :
# montrer le vrai produit) MAIS secrets d'EXEMPLE (jamais réels — le rendu ne
# touche jamais `property_secrets`), demandes désactivées, bandeau « Aperçu ».

@router.get("/v/{showcase_token}", response_class=HTMLResponse)
def public_showcase_page(showcase_token: str, conn: Conn, request: Request,
                         lang: str | None = None):
    """Page vitrine d'un logement publié (prospect/annonce/démo). 404 propre si le
    token est inconnu / non publié. Ne montre JAMAIS un secret réel."""
    prop = repo.get_property_by_showcase_token(conn, showcase_token)
    if not prop:
        # Même page neutre que le lien de séjour mort (§1.2/§1.5) : les cas morts
        # des NOUVEAUX préfixes (`/b/` et `/v/`) servent la même page — jamais un
        # guide, jamais une donnée du logement.
        return HTMLResponse(guide_page.render_stay_expired(), status_code=404,
                            headers=_NOINDEX)
    _p, sections, pois, area_facts, property_media, effective, lang_names = \
        _assemble_guide(conn, prop, lang, media_base=f"/v/{showcase_token}")
    base = _base_url(request)
    photo = _first_photo_path(sections, property_media, showcase_token)
    og_image_url = base + (photo or f"/v/{showcase_token}/og-image.png")
    ui_overlay = repo.ui_translations(conn, effective)
    # Watermark : même règle que le guide (plan gratuit → marque « Créé avec
    # Holaguia »), résolu par le propriétaire du logement de la vitrine.
    watermark = plans.wants_watermark(plans.get_plan(conn, str(prop["owner_id"])))
    html = guide_page.render_guide(_property_public(prop), sections, pois,
                                   area_facts, showcase_token, lang=effective,
                                   base_url=base, og_image_url=og_image_url,
                                   watermark=watermark, lang_names=lang_names,
                                   ui_overlay=ui_overlay, variant="showcase",
                                   canonical_path=f"/v/{showcase_token}",
                                   manifest=False)
    return HTMLResponse(html, headers=_public_headers())


@router.get("/v/{showcase_token}/og-image.png")
def showcase_og_image(showcase_token: str, conn: Conn):
    """Image de marque de la vitrine (aucune photo). 404 si non publié."""
    prop = repo.get_property_by_showcase_token(conn, showcase_token)
    if not prop:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Introuvable")
    place = ", ".join(x for x in [prop.get("city"), prop.get("region")] if x)
    png = og_image.build_og_image(prop["name"], subtitle=place)
    return Response(content=png, media_type="image/png",
                    headers=_public_headers())


@router.get("/v/{showcase_token}/media/{media_id}")
def showcase_media(showcase_token: str, media_id: str, conn: Conn):
    """Sert un média d'une vitrine — mêmes garanties de visibilité que le guide
    (section visible & 'guest', ou média du logement). 404 sinon. Le vrai
    `guide_token` ne transite jamais par la vitrine (progrès de sécurité)."""
    row = repo.get_showcase_media(conn, showcase_token, media_id)
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


@router.get("/g/{guide_token}/data")
def public_guide_data(guide_token: str, conn: Conn, lang: str | None = None):
    """Guide JSON pré-calculé (sans aucun secret) pour l'app / usages tiers.
    Le contenu est renvoyé dans `lang` si c'est une langue publiée (M-09).
    Accepte aussi le lien de partage `/g/{slug}-{token}/data` (M-25)."""
    loaded = _load_guide(conn, _real_token(guide_token), lang)
    if not loaded:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Guide introuvable")
    prop, sections, pois, area_facts, property_media, effective, lang_names = loaded
    pub = _property_public(prop)
    # Le registre fait foi (V2-21a) : n'exposer que les langues publiées ET
    # présentes dans le registre — une langue dépubliée globalement disparaît
    # aussi du JSON, jamais un sélecteur tiers ne la proposera.
    pub["published_langs"] = [l for l in (pub.get("published_langs") or [])
                              if l in lang_names]
    return _json({
        "property": pub,
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


# ── Demande de service du voyageur (V2-23b, volet 3, §3.1) ───────────────────

def _send_email_bg(background: BackgroundTasks, mailer, to: str, email) -> None:
    """Envoi best-effort en tâche de fond (même garde-fou que auth._send_email_bg,
    V2-16) : une exception d'une `BackgroundTask` annulerait la transaction de la
    requête (rollback) — on l'avale donc, journalisée. La demande est déjà
    committée avant de programmer la tâche (V2-16b)."""
    def _run() -> None:
        try:
            mailer.send(to, email)
        except Exception:  # noqa: BLE001 — best-effort : jamais bloquant
            log.warning("Notification de demande vers %s échouée (ignorée).",
                        to, exc_info=True)
    background.add_task(_run)


def _stay_label(booking: dict) -> str:
    """« du 12/08 au 20/08 » — repère de séjour pour l'email au propriétaire."""
    return (f"du {booking['starts_on'].strftime('%d/%m')} au "
            f"{booking['ends_on'].strftime('%d/%m/%Y')}")


@router.post("/g/{guide_token}/requests", response_model=GuestServiceRequestOut)
def guest_service_request(guide_token: str, payload: GuestServiceRequestIn,
                          conn: Conn, request: Request,
                          background: BackgroundTasks, mailer: Mailer):
    """Demande d'un service annoncé « sur demande » dans le guide (ménage/draps
    supplémentaires, service…). Atterrit dans le planning plutôt que dans un SMS
    oublié : crée une `booking_requests` en `origin='guest'`, `status='pending'`,
    rattachée au séjour **en cours** (à défaut au suivant), et notifie le
    propriétaire (il accepte/refuse ; acceptée → intervention visible par l'équipe).

    Le voyageur n'est pas authentifié → **rate-limit par guide** (anti-abus). Le
    libellé vient TOUJOURS du template de la section (jamais d'une valeur libre).
    Invariant 4 préservé : action déclenchée par le voyageur, aucun appel externe
    automatique au rendu."""
    token = _real_token(guide_token)
    # 1. La section doit réellement offrir ce service sur CE guide (visible, guest,
    #    requestable) — sinon rien ne fait foi et on ne révèle rien de plus.
    label = repo.requestable_section_label(conn, token, payload.section)
    if not label:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Ce service n'est pas proposé sur ce guide.")
    # 2. Rattachement au séjour. Lien de SÉJOUR (V2-23c) : rattachement CERTAIN au
    #    séjour du `stay_token` (le token désigne le séjour du logement — plus de
    #    devinette). Lien maison : repli sur le séjour occupé en cours (à défaut,
    #    le suivant).
    if payload.stay_token:
        # Rattachement CERTAIN par le `stay_token` seul (V2-23c amendé) : le token
        # désigne le séjour ET son logement — plus aucune dépendance au
        # `guide_token` de l'URL, plus aucune devinette.
        stay = _resolve_stay(conn, payload.stay_token)
        if not stay:
            # Token de séjour cassé/expiré/annulé : on ne devine pas (ce serait
            # rattacher au séjour d'un autre) et on ne révèle rien.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Ce lien de séjour n'est plus actif.")
        booking = repo.get_booking(conn, str(stay["property_id"]), str(stay["id"]))
    else:
        booking = repo.current_or_next_booking_by_guide_token(
            conn, token, _dt.date.today())
    if not booking:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aucun séjour n'est enregistré pour le moment. "
                   "Contactez directement votre hôte.")
    pid = str(booking["property_id"])
    # 3. Anti-abus : cadence minimale par guide (le voyageur n'est pas authentifié).
    since = repo.seconds_since_last_guest_request(conn, pid)
    if since is not None and since < settings.guest_request_min_interval_s:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Une demande vient d'être envoyée. Patientez quelques minutes "
                   "avant d'en envoyer une autre.")
    note = (payload.note or "").strip() or None
    repo.create_booking_request(
        conn, str(booking["id"]), request_type_id=None, label=label,
        quantity=1, note=note, origin="guest", status="pending")
    # V2-16b : committer AVANT de programmer la tâche de fond (l'email lent ne doit
    # pas retarder la visibilité de la demande).
    conn.commit()
    # 4. Notifier le propriétaire (best-effort — jamais bloquant, jamais de rollback).
    owner = repo.get_owner_by_property(conn, pid)
    if owner and owner.get("email"):
        base = (settings.public_base_url or str(request.base_url)).rstrip("/")
        prop = repo.get_published_property_by_token(conn, token)
        email = emails.guest_service_request_email(
            property_name=(prop["name"] if prop else "votre logement"),
            service_label=label, note=note,
            guest_name=booking.get("guest_name"),
            stay_label=_stay_label(booking),
            calendar_url=f"{base}/#/properties/{pid}/calendrier",
            full_name=owner.get("full_name"))
        _send_email_bg(background, mailer, owner["email"], email)
    return GuestServiceRequestOut(
        label=label,
        message="Votre demande a bien été transmise à votre hôte.")


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
    # Gating V2-18b : le cahier équipe est réservé à l'offre Pro (aperçu pendant
    # l'essai, grand-père pour les comptes existants). Sinon, page sobre d'upsell
    # (jamais d'erreur brute — un membre du staff peut tomber dessus).
    if not plans.effective_entitlements(conn, str(prop["owner_id"]))["staff_access"]:
        return HTMLResponse(guide_page.render_staff_locked(prop),
                            headers=_public_headers(no_store=True))
    planning = _staff_planning(conn, prop)
    html = guide_page.render_staff(prop, sections, staff_token, planning=planning)
    return HTMLResponse(html, headers=_public_headers(no_store=True))


def _staff_planning(conn, prop: dict) -> list[dict]:
    """Frise du cahier d'équipe (V2-23b, §2), calculée à la lecture (jamais
    stockée, comme les fenêtres). Fenêtres de préparation, interventions en cours
    de séjour (avec coordonnées **seulement** pour les séjours en cours/à venir —
    RGPD), séjours non occupés grisés. La nature pilote la préparation (invariant
    14) ; le gating Pro est déjà assuré par `staff_access` en amont."""
    pid = str(prop["id"])
    bookings = repo.list_bookings(conn, pid)
    reqs_by_booking: dict[str, list] = {}
    for r in repo.list_requests_for_property(conn, pid):
        reqs_by_booking.setdefault(str(r["booking_id"]), []).append(r)
    return care.build_planning(
        bookings, prop.get("care_rules") or {},
        prop["default_checkin_time"], prop["default_checkout_time"],
        today=_dt.date.today(), requests_by_booking=reqs_by_booking)


@router.get("/s/{staff_token}/media/{media_id}")
def staff_media_file(staff_token: str, media_id: str, conn: Conn):
    """Sert un média d'un cahier 'staff' — uniquement une section 'staff' visible.
    404 sinon, sans rien révéler (jamais un média 'guest' ni de section masquée).
    Gating Pro (V2-18b) : si le plan n'inclut pas le guide équipe, 404 (on ne
    révèle rien de plus que la page verrouillée)."""
    prop = repo.get_property_by_staff_token(conn, staff_token)
    if prop and not plans.effective_entitlements(
            conn, str(prop["owner_id"]))["staff_access"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Média introuvable")
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
