"""CRUD des logements, données sensibles chiffrées et sections du guide (§3.1)."""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Request,
                     Response, status)

from .. import crypto, poster, repo, wifi
from ..config import settings
from ..deps import (
    Conn, CurrentOwner, DistanceComputer, Geocoder, OwnedProperty,
    TranslationRunner, get_distance_computer, get_geocoder,
    get_translation_runner,
)
from ..schemas import (
    GeocodeOut, PropertyIn, PropertyOut, PropertyStatsOut, PropertyUpdate,
    RecomputeOut, SecretsIn, SecretsOut, SectionUpsertIn, WifiNetworkOut,
)
from enrich.geocode import GeocodeError
from .enrich import schedule_translation

router = APIRouter(prefix="/api/properties", tags=["properties"])


def _slug(name: str) -> str:
    """Nom de fichier ASCII sûr dérivé du nom du logement (pour le PDF)."""
    ascii_name = (unicodedata.normalize("NFKD", name)
                  .encode("ascii", "ignore").decode())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
    return slug or "guide"


# ── CRUD logements ───────────────────────────────────────────────────────────

@router.get("", response_model=list[PropertyOut])
def list_properties(conn: Conn, owner: CurrentOwner):
    return repo.list_properties(conn, str(owner["id"]))


@router.post("", response_model=PropertyOut, status_code=status.HTTP_201_CREATED)
def create_property(payload: PropertyIn, conn: Conn, owner: CurrentOwner):
    # Limite de logements selon le plan (§10)
    plan = repo.get_owner_plan(conn, str(owner["id"]))
    if plan and plan["max_properties"] is not None:
        if repo.count_properties(conn, str(owner["id"])) >= plan["max_properties"]:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Limite de logements atteinte pour le plan « {plan['name']} »")
    data = payload.model_dump()
    data["country_code"] = data["country_code"].upper()
    return repo.create_property(conn, str(owner["id"]), data)


@router.get("/{property_id}", response_model=PropertyOut)
def get_property(prop: OwnedProperty):
    return prop


@router.patch("/{property_id}", response_model=PropertyOut)
def update_property(
    payload: PropertyUpdate, conn: Conn, owner: CurrentOwner,
    prop: OwnedProperty, background: BackgroundTasks,
    runner: Annotated[TranslationRunner, Depends(get_translation_runner)],
):
    fields = payload.model_dump(exclude_unset=True)
    if "country_code" in fields and fields["country_code"]:
        fields["country_code"] = fields["country_code"].upper()
    updated = repo.update_property(conn, str(owner["id"]), str(prop["id"]), fields)
    # À la (re)publication : (re)traduire ce qui manque ou est périmé (M-09, §9).
    # Tâche de fond — n'allonge pas la réponse ; ciblage côté pipeline.
    if fields.get("status") == "published":
        schedule_translation(background, conn, updated, runner)
    return updated


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_property(conn: Conn, owner: CurrentOwner, prop: OwnedProperty):
    repo.delete_property(conn, str(owner["id"]), str(prop["id"]))
    return None


@router.get("/{property_id}/stats", response_model=PropertyStatsOut)
def property_stats(conn: Conn, prop: OwnedProperty):
    """Complétude des sections + décompte des POI par statut (tableau de bord)."""
    return repo.property_stats(conn, str(prop["id"]))


# ── Repositionnement du logement : recalcul des distances (§5.1, M-05) ───────

@router.post("/{property_id}/recompute-distances", response_model=RecomputeOut)
def recompute_distances(
    conn: Conn, prop: OwnedProperty,
    computer: Annotated[DistanceComputer, Depends(get_distance_computer)],
):
    """Recalcule les distances/temps de tous les POI depuis la position courante
    du logement. Appelé après un placement manuel du point sur la carte : les
    distances pré-calculées deviennent cohérentes avec la nouvelle position.

    N'altère que les colonnes de distance (jamais le statut ni le contenu
    arbitré par le propriétaire — invariant 1)."""
    if prop["lat"] is None or prop["lon"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le logement n'a pas encore de position géographique")
    return RecomputeOut(updated=_recompute_distances(conn, prop, computer))


def _recompute_distances(conn, prop: dict, computer: DistanceComputer) -> int:
    """Recalcule les distances de tous les POI depuis la position de `prop`.
    Ne touche que les colonnes de distance (invariant 1). Renvoie le nombre de
    POI mis à jour."""
    pois = repo.list_poi_positions(conn, str(prop["id"]))
    if not pois:
        return 0
    computer((prop["lat"], prop["lon"]), pois)
    for p in pois:
        repo.update_poi_distances(
            conn, str(p["id"]),
            dist_walk_m=p.get("dist_walk_m"), walk_min=p.get("walk_min"),
            dist_drive_m=p.get("dist_drive_m"), drive_min=p.get("drive_min"))
    return len(pois)


# ── (Re)géocodage explicite de l'adresse (§5.1, M-24) ────────────────────────

@router.post("/{property_id}/geocode", response_model=GeocodeOut)
def geocode_property(
    conn: Conn, owner: CurrentOwner, prop: OwnedProperty,
    geocoder: Annotated[Geocoder, Depends(get_geocoder)],
    computer: Annotated[DistanceComputer, Depends(get_distance_computer)],
):
    """(Re)géocode l'adresse actuelle du logement et recalcule les distances.

    Action **explicite** du propriétaire, jamais automatique (M-24) : elle
    remplace la position à partir de l'adresse (`geocode_source='nominatim'`).
    Le front ne l'appelle qu'après accord (case décochée par défaut lorsque la
    position a été placée à la main). 422 si l'adresse reste introuvable — la
    position existante est alors préservée."""
    try:
        res = geocoder(prop)
    except GeocodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Adresse introuvable : vérifiez la rue, le code postal et la "
                   "ville, ou placez le point à la main sur la carte.")
    updated = repo.set_geocode(
        conn, str(owner["id"]), str(prop["id"]),
        lat=res["lat"], lon=res["lon"], accuracy=res.get("accuracy") or "city",
        source=res.get("source") or "nominatim")
    n = _recompute_distances(conn, updated, computer)
    return GeocodeOut(property=updated, accuracy=updated["geocode_accuracy"],
                      distances_updated=n)


# ── Affiche « QR code à imprimer » du guide (§3.2, M-07) ─────────────────────

@router.get("/{property_id}/guide-poster.pdf")
def guide_poster(request: Request, prop: OwnedProperty,
                 size: Literal["a5", "a4"] = "a5",
                 lang: Literal["fr", "en", "es"] = "fr"):
    """PDF imprimable (A5 par défaut, `?size=a4`) : nom du logement + QR du lien
    du guide + mot d'accueil localisé (`?lang=fr|en|es`, M-26). Réservé au
    propriétaire du logement (via `OwnedProperty`). N'encode que le lien public
    `/g/{guide_token}` — jamais un secret. L'origine des liens vient de
    `CASAGUIDE_PUBLIC_BASE_URL` sinon de la requête."""
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    guide_url = f"{base}/g/{prop['guide_token']}"
    pdf = poster.build_guide_poster(
        property_name=prop["name"], guide_url=guide_url,
        city=prop.get("city"), size=size, lang=lang)
    filename = f"casaguide-qr-{_slug(prop['name'])}-{lang}.pdf"
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Données sensibles (chiffrées AES-GCM, invariant 5) ───────────────────────

def _secrets_out(networks: list[dict], keybox_code: str | None,
                 keybox_notes: str | None) -> SecretsOut:
    """Vue propriétaire des secrets : liste multi-wifi + champs legacy alimentés
    depuis le réseau n°1 (rétrocompatibilité, M-15)."""
    net1 = wifi.first_network(networks)
    return SecretsOut(
        wifi_networks=[WifiNetworkOut(label=n["label"], ssid=n["ssid"],
                                      password=n["pass"]) for n in networks],
        wifi_ssid=net1["ssid"] if net1 else None,
        wifi_pass=net1["pass"] if net1 else None,
        keybox_code=keybox_code, keybox_notes=keybox_notes)


def _incoming_networks(payload: SecretsIn) -> list[dict]:
    """Réseaux entrants normalisés. Rétrocompat : si seuls les anciens champs
    wifi_ssid/wifi_pass sont fournis, ils forment un réseau unique (n°1)."""
    if payload.wifi_networks is not None:
        return wifi.clean_networks(
            [{"label": n.label, "ssid": n.ssid, "pass": n.password}
             for n in payload.wifi_networks])
    if payload.wifi_ssid or payload.wifi_pass:
        return wifi.clean_networks(
            [{"label": wifi.DEFAULT_LABEL, "ssid": payload.wifi_ssid,
              "pass": payload.wifi_pass}])
    return []


@router.put("/{property_id}/secrets", response_model=SecretsOut)
def set_secrets(payload: SecretsIn, conn: Conn, prop: OwnedProperty):
    if not crypto.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chiffrement non configuré (CASAGUIDE_SECRET_KEY absente)")
    networks = _incoming_networks(payload)
    net1 = wifi.first_network(networks)
    repo.upsert_secrets(
        conn, str(prop["id"]),
        # Colonnes legacy en miroir du réseau n°1 (rétrocompat, M-15)
        wifi_ssid=net1["ssid"] if net1 else None,
        wifi_pass_enc=crypto.encrypt(net1["pass"]) if net1 and net1["pass"] else None,
        wifi_networks_enc=wifi.encrypt_networks(networks),
        keybox_code_enc=crypto.encrypt(payload.keybox_code) if payload.keybox_code else None,
        keybox_notes=payload.keybox_notes,
    )
    return _secrets_out(networks, payload.keybox_code, payload.keybox_notes)


@router.get("/{property_id}/secrets", response_model=SecretsOut)
def get_secrets(conn: Conn, prop: OwnedProperty):
    """Déchiffrement réservé au propriétaire authentifié (jamais côté voyageur)."""
    if not crypto.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chiffrement non configuré (CASAGUIDE_SECRET_KEY absente)")
    row = repo.get_secrets(conn, str(prop["id"]))
    if not row:
        return SecretsOut()
    return _secrets_out(wifi.networks_from_row(row),
                        crypto.decrypt(row["keybox_code_enc"]),
                        row["keybox_notes"])


# ── Sections du guide (contenu saisi par le propriétaire) ────────────────────

@router.get("/{property_id}/sections")
def list_sections(conn: Conn, prop: OwnedProperty):
    """Catalogue complet des sections pré-définies + contenu déjà saisi.
    Sert de base au formulaire guidé du back-office (§4)."""
    rows = repo.list_sections_with_templates(conn, str(prop["id"]))
    total = len(rows)
    done = sum(1 for r in rows if r["completed"])
    return {
        "completion_pct": round(done / total * 100) if total else 0,
        "sections": rows,
    }


@router.put("/{property_id}/sections/{template_code}")
def upsert_section(template_code: str, payload: SectionUpsertIn, conn: Conn,
                   prop: OwnedProperty):
    if not repo.section_template_exists(conn, template_code):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Section inconnue : {template_code}")
    return repo.upsert_section(
        conn, str(prop["id"]), template_code,
        content=payload.content, body_md=payload.body_md,
        is_visible=payload.is_visible, completed=payload.completed)
