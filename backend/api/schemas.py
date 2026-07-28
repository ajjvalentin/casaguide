"""Schémas Pydantic (validation des requêtes / forme des réponses)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ── Auth ─────────────────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    company_name: str | None = None
    phone: str | None = None
    locale: str = "fr"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=8, max_length=200)


class VerifyIn(BaseModel):
    token: str = Field(min_length=1)


class MessageOut(BaseModel):
    """Réponse neutre (mot de passe oublié / vérification) — jamais d'info
    révélant l'existence d'un compte."""
    message: str


class OwnerOut(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    company_name: str | None = None
    phone: str | None = None
    locale: str
    email_verified: bool = False
    plan_id: str | None = None
    # État d'essai (V2-18a) : pilote la page « Mon abonnement » et le bandeau de
    # lecture seule du back-office. `trial_expired` ⇒ écritures refusées (403).
    on_trial: bool = False
    trial_expired: bool = False
    trial_ends_at: datetime | None = None
    # Accès au cahier de l'équipe d'entretien /s/ (V2-18b) : pilote la « double
    # porte » de la carte du logement (V2-26). false ⇒ porte badgée « Pro » +
    # encart d'upsell au clic ; true (Pro/grand-périsé, ou aperçu d'essai) ⇒
    # porte fonctionnelle (aperçu signalé via `on_trial`).
    staff_access: bool = False


# ── Plans & abonnement (V2-05a) ──────────────────────────────────────────────

class PlanOut(BaseModel):
    """Un plan du catalogue (source : table `plans`, jamais de prix en dur)."""
    id: str
    name: str
    max_properties: int | None = None    # None = illimité
    enrich_quota: int
    price_month_cts: int
    # Prix mensuel (centimes) d'un logement supplémentaire (add-on Pro, V2-18b) ;
    # None si le plan n'a pas d'add-on.
    addon_property_price_cts: int | None = None
    features: dict[str, Any] = {}


class QuotaGaugeOut(BaseModel):
    """Une jauge d'utilisation. `limit is None` ⇒ illimité."""
    used: int
    limit: int | None = None


class UsageOut(BaseModel):
    properties: QuotaGaugeOut            # logements créés vs max_properties
    enrichments: QuotaGaugeOut           # enrichissements du mois (tous logements)
    langs: QuotaGaugeOut                 # langues publiées (source comprise) vs plafond


class ScheduledChangeOut(BaseModel):
    """Changement d'offre PROGRAMMÉ à l'échéance (downgrade, V2-18e). Alimente le
    bandeau « <offre> à partir du JJ/MM — Annuler ». La vérité vient du webhook."""
    plan: str                            # id de l'offre cible (ex. 'solo')
    plan_name: str                       # nom lisible de l'offre cible
    effective_at: datetime | None = None # date d'effet (fin de période en cours)


class SubscriptionOut(BaseModel):
    plan: PlanOut
    status: str
    usage: UsageOut
    # Client Stripe déjà rattaché ? (pilote l'affichage du bouton « Gérer mon
    # abonnement » côté front — V2-05b). Absent tant qu'aucun paiement.
    has_stripe_customer: bool = False
    # État d'essai (V2-18a) : pilote la page « Mon abonnement » (compte à rebours,
    # bandeau d'expiration, CTA « Choisir mon offre »).
    on_trial: bool = False
    trial_expired: bool = False
    trial_ends_at: datetime | None = None
    # Logements supplémentaires actifs (add-on Pro, V2-18b) — pilote le stepper.
    addon_qty: int = 0
    # Fin de la période payée en cours (V2-18e) : date de référence des dialogues
    # de changement d'offre (prorata jusqu'au…, downgrade à partir du…).
    current_period_end: datetime | None = None
    # Downgrade programmé à l'échéance (V2-18e), ou None si aucun.
    scheduled_change: ScheduledChangeOut | None = None


# ── Paiement Stripe (V2-05b) ─────────────────────────────────────────────────

class CheckoutIn(BaseModel):
    """Demande de session Checkout pour un plan payant."""
    plan: Literal["solo", "pro"]


class CheckoutOut(BaseModel):
    """URL de redirection vers le Checkout hébergé Stripe."""
    url: str


class PortalOut(BaseModel):
    """URL de redirection vers le portail client Stripe (cartes, factures, annulation)."""
    url: str


class AddonIn(BaseModel):
    """Demande de quantité de logements supplémentaires (add-on Pro, V2-18b).
    0 = supprimer l'add-on. Plafond souple pour éviter une faute de frappe."""
    quantity: int = Field(ge=0, le=100)


class AddonOut(BaseModel):
    """Accusé de la demande d'add-on : la quantité DEMANDÉE et l'état « en cours »
    (le webhook Stripe posera la quantité effective — seule autorité, invariant 1)."""
    requested_quantity: int
    status: str = "pending"


class ChangePlanIn(BaseModel):
    """Changement d'offre pour un abonné payant **déjà actif** (V2-18d). Modifie
    l'abonnement Stripe EXISTANT (proration) au lieu d'ouvrir un nouveau Checkout."""
    plan: Literal["solo", "pro"]


class ChangePlanOut(BaseModel):
    """Accusé du changement d'offre : l'offre cible demandée, en attente de la
    confirmation du webhook (seule autorité, invariants 9/12). Le front affiche
    « mise à jour en cours » jusque-là.

    `direction` (V2-18e) : 'upgrade' (effet IMMÉDIAT, prorata) ou 'downgrade'
    (effet À L'ÉCHÉANCE). `effective_at` = date d'effet d'un downgrade (fin de
    période) ; None pour un upgrade (immédiat)."""
    target_plan: str
    status: str = "pending"
    direction: Literal["upgrade", "downgrade"] = "upgrade"
    effective_at: datetime | None = None


class CancelScheduledOut(BaseModel):
    """Accusé de l'annulation d'un changement programmé (V2-18e) : la demande est
    envoyée à Stripe, l'effacement effectif revient par le webhook
    `subscription_schedule.released` (seule autorité, invariant 12)."""
    status: str = "pending"


# ── Logements ────────────────────────────────────────────────────────────────

class PropertyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address_line1: str = Field(min_length=1, max_length=300)
    address_line2: str | None = None
    postal_code: str | None = None
    city: str = Field(min_length=1, max_length=200)
    region: str | None = None
    country_code: str = Field(min_length=2, max_length=2)
    default_lang: str = "fr"
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_whatsapp: str | None = None
    contact_email: str | None = None
    contact_backup: str | None = None
    tourism_license: str | None = None


class PropertyUpdate(BaseModel):
    """Tous les champs optionnels : mise à jour partielle (PATCH)."""
    name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    postal_code: str | None = None
    city: str | None = None
    region: str | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    default_lang: str | None = None
    access_mode: Literal["link", "pin", "stay_dates"] | None = None
    status: Literal["draft", "published", "archived"] | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_whatsapp: str | None = None
    contact_email: str | None = None
    contact_backup: str | None = None
    tourism_license: str | None = None
    # Placement manuel du point sur la carte (§5.1 : le propriétaire corrige le géocodage)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


class PropertyOut(BaseModel):
    id: UUID
    name: str
    address_line1: str
    address_line2: str | None = None
    postal_code: str | None = None
    city: str
    region: str | None = None
    country_code: str
    lat: float | None = None
    lon: float | None = None
    geocode_source: str | None = None
    geocode_accuracy: str | None = None
    guide_token: str
    staff_token: str          # lien du cahier équipe d'entretien (/s/…, M-13)
    access_mode: str
    status: str
    default_lang: str
    published_langs: list[str] = []
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_whatsapp: str | None = None
    contact_email: str | None = None
    contact_backup: str | None = None
    tourism_license: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Données sensibles ────────────────────────────────────────────────────────

class WifiNetworkIn(BaseModel):
    """Un réseau wifi entrant (M-15). Le champ JSON est « pass » (aliasé)."""
    model_config = ConfigDict(populate_by_name=True)
    label: str | None = Field(default=None, max_length=80)
    ssid: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, alias="pass", max_length=200)


class WifiNetworkOut(BaseModel):
    """Un réseau wifi sortant (M-15). Sérialisé avec la clé « pass »."""
    model_config = ConfigDict(populate_by_name=True)
    label: str
    ssid: str | None = None
    password: str | None = Field(default=None, alias="pass")


class SecretsIn(BaseModel):
    # Multi-wifi (M-15) : liste de réseaux. Les champs simples wifi_ssid/wifi_pass
    # restent acceptés (rétrocompat) et sont traités comme un réseau unique.
    wifi_networks: list[WifiNetworkIn] | None = None
    wifi_ssid: str | None = None
    wifi_pass: str | None = None      # sera chiffré avant stockage
    keybox_code: str | None = None    # sera chiffré avant stockage
    keybox_notes: str | None = None


class SecretsOut(BaseModel):
    """Renvoyé uniquement au propriétaire authentifié (jamais au voyageur).
    Expose la liste multi-wifi (M-15) ET les anciens champs alimentés depuis le
    réseau n°1 (pour ne rien casser)."""
    wifi_networks: list[WifiNetworkOut] = []
    wifi_ssid: str | None = None
    wifi_pass: str | None = None
    keybox_code: str | None = None
    keybox_notes: str | None = None


# ── Sections ─────────────────────────────────────────────────────────────────

class SectionUpsertIn(BaseModel):
    content: dict[str, Any] = {}
    body_md: str | None = None
    is_visible: bool = True
    completed: bool = False


# ── POI (validation par le propriétaire) ─────────────────────────────────────

class PoiEditIn(BaseModel):
    """Édition d'un POI suggéré → passe le POI en statut 'edited'."""
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    opening_hours: str | None = None
    cuisine: str | None = None
    description_md: str | None = None
    owner_comment: str | None = None


class PoiStatusIn(BaseModel):
    """Positionne explicitement le statut d'un POI (annulation réversible, M-23)."""
    status: Literal["suggested", "approved", "edited", "rejected"]


class PoiCandidateOut(BaseModel):
    """Candidat renvoyé par la recherche Nominatim (M-22) — jamais persisté tel
    quel : le propriétaire l'édite puis valide via POST /pois."""
    name: str
    address: str | None = None
    lat: float
    lon: float
    category_code: str
    phone: str | None = None
    website: str | None = None


class PoiCreateIn(BaseModel):
    """Création manuelle d'un POI par le propriétaire (M-22) → source='owner',
    status='approved' (jamais écrasé par un ré-enrichissement, invariant 1)."""
    category_code: str
    name: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    opening_hours: str | None = None
    cuisine: str | None = None
    description_md: str | None = None
    owner_comment: str | None = None


# ── Médias (photos / PDF par section, M-12) ──────────────────────────────────

class MediaOut(BaseModel):
    id: UUID
    section_code: str | None = None
    kind: str
    caption: str | None = None
    sort_order: int
    url: str                       # endpoint de service (propriétaire authentifié)
    created_at: datetime


class MediaCaptionIn(BaseModel):
    caption: str | None = Field(default=None, max_length=500)


class MediaReorderIn(BaseModel):
    ids: list[UUID]


# ── Indicateurs (« Mes logements » et éditeur) ───────────────────────────────

class PropertyStatsOut(BaseModel):
    sections_total: int
    sections_done: int
    sections_visible: int
    completion_pct: int
    pois_total: int
    pois_suggested: int
    pois_approved: int
    pois_edited: int
    pois_rejected: int


class RecomputeOut(BaseModel):
    updated: int


class GeocodeOut(BaseModel):
    """Résultat d'un (re)géocodage explicite de l'adresse (M-24)."""
    property: PropertyOut
    accuracy: str
    distances_updated: int


# ── Enrichissement ───────────────────────────────────────────────────────────

class EnrichIn(BaseModel):
    trigger: Literal["initial", "refresh", "manual"] = "manual"


class JobOut(BaseModel):
    id: UUID
    trigger: str
    status: str
    steps: dict[str, Any] = {}
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
