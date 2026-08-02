"""Schémas Pydantic (validation des requêtes / forme des réponses)."""
from __future__ import annotations

from datetime import date, datetime, time
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
    # Heures standard du calendrier des séjours (V2-23a)
    default_checkin_time: time | None = None
    default_checkout_time: time | None = None
    # Règles d'entretien (V2-23b, §1.1) — objet JSON libre, remplacé en bloc.
    care_rules: dict | None = None
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
    default_checkin_time: time | None = None
    default_checkout_time: time | None = None
    care_rules: dict = {}     # règles d'entretien (V2-23b, §1.1)
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


# ── Calendrier des séjours (V2-23a) ──────────────────────────────────────────

_Platform = Literal["airbnb", "vrbo", "booking", "other"]
_Source = Literal["airbnb", "vrbo", "booking", "direct", "other"]
# Cycle de vie (V2-23b) : distinct de la sémantique (nature).
_BookingStatus = Literal["active", "cancelled"]
# Sémantique du séjour : c'est elle qui pilote la préparation (jamais le statut).
_Nature = Literal["reservation", "private", "works", "unavailable", "unqualified"]


class CalendarIn(BaseModel):
    """Ajout d'un flux iCal (validation immédiate au collage)."""
    platform: _Platform = "other"
    ical_url: str = Field(min_length=8, max_length=2000)


class CalendarOut(BaseModel):
    """Vue d'un flux : URL **masquée** (jamais l'URL en clair — c'est un secret)."""
    id: UUID
    platform: str
    masked_url: str
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    sync_error: str | None = None
    created_at: datetime


class SyncResultOut(BaseModel):
    status: str
    error: str | None = None
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    total: int = 0


class CalendarCreateOut(BaseModel):
    """Réponse au collage d'une URL : le flux créé + le bilan de la 1re synchro."""
    calendar: CalendarOut
    sync: SyncResultOut


class SyncNowOut(BaseModel):
    """Bilan agrégé d'un « Synchroniser maintenant » (tous les flux du logement)."""
    calendars: int
    ok: int
    errors: int
    created: int
    updated: int
    cancelled: int


class BookingIn(BaseModel):
    """Saisie directe d'un séjour. `nature` porte la sémantique (défaut :
    réservation) ; le cycle de vie 'active' est implicite."""
    starts_on: date
    ends_on: date
    checkin_time: time | None = None
    checkout_time: time | None = None
    luggage_drop_time: time | None = None
    luggage_until_time: time | None = None
    source: _Source = "direct"
    guest_name: str | None = Field(default=None, max_length=200)
    guest_contact: str | None = Field(default=None, max_length=200)
    # Coordonnées séparées (V2-23b, §3.0) : téléphone (action tel:/WhatsApp), email
    # (mailto:, lien du guide), langue du locataire (?lang=xx).
    guest_phone: str | None = Field(default=None, max_length=60)
    guest_email: str | None = Field(default=None, max_length=200)
    guest_lang: str | None = Field(default=None, max_length=10)
    notes: str | None = None
    nature: _Nature = "reservation"
    # Voyageurs (V2-23b, §1.0) — le nombre d'enfants se déduit de children_ages.
    guest_count: int | None = Field(default=None, ge=0, le=100)
    children_ages: list[int] | None = None


class BookingUpdate(BaseModel):
    """Complétion / édition d'un séjour (tous champs optionnels). Sert à qualifier
    un import 'unqualified' (nature), à rattacher un bloc miroir
    (`linked_booking_id`), et à annuler/réactiver (status)."""
    starts_on: date | None = None
    ends_on: date | None = None
    checkin_time: time | None = None
    checkout_time: time | None = None
    luggage_drop_time: time | None = None
    luggage_until_time: time | None = None
    guest_name: str | None = Field(default=None, max_length=200)
    guest_contact: str | None = Field(default=None, max_length=200)
    guest_phone: str | None = Field(default=None, max_length=60)
    guest_email: str | None = Field(default=None, max_length=200)
    guest_lang: str | None = Field(default=None, max_length=10)
    notes: str | None = None
    nature: _Nature | None = None
    status: _BookingStatus | None = None
    linked_booking_id: UUID | None = None
    guest_count: int | None = Field(default=None, ge=0, le=100)
    children_ages: list[int] | None = None


class BookingOut(BaseModel):
    id: UUID
    calendar_id: UUID | None = None
    starts_on: date
    ends_on: date
    checkin_time: time | None = None          # NULL = heure standard héritée
    checkout_time: time | None = None
    eff_checkin_time: time                     # heure effective (héritée ou ajustée)
    eff_checkout_time: time
    luggage_drop_time: time | None = None
    luggage_until_time: time | None = None
    source: str
    external_uid: str | None = None
    is_direct: bool                            # saisie directe (éditable/supprimable)
    guest_name: str | None = None
    guest_contact: str | None = None           # legacy fourre-tout (repli d'affichage)
    guest_phone: str | None = None             # téléphone (action tel:/WhatsApp, §3.0)
    guest_email: str | None = None             # email (mailto:, lien du guide)
    guest_lang: str | None = None              # langue du locataire (?lang=xx)
    notes: str | None = None
    nature: str                                # sémantique (pilote la préparation)
    status: str                                # cycle de vie ('active' | 'cancelled')
    linked_booking_id: UUID | None = None      # bloc miroir rattaché à un autre séjour
    guest_count: int | None = None             # voyageurs (§1.0)
    children_count: int = 0                     # dérivé de children_ages
    children_ages: list[int] = []
    # Demandes du voyageur EN ATTENTE rattachées à ce séjour (§3.1) : badge dans le
    # calendrier → le propriétaire accepte/refuse. 0 pour un séjour sans demande.
    pending_guest_requests: int = 0
    # Relance active (§0.6) : {code, message} pour ce séjour (voyageurs/coordonnées
    # manquants, nature à qualifier). Vide si rien à signaler.
    missing_info: list[dict] = []


class OverlapOut(BaseModel):
    """Deux séjours confirmés qui se recouvrent (alerte, jamais un blocage)."""
    a: UUID
    b: UUID


class RotationOut(BaseModel):
    """Rotation même jour : un départ puis une arrivée, avec la fenêtre de prépa."""
    on: date
    departing: UUID
    arriving: UUID
    gap_minutes: int
    # Signal de rotation gradué (§2.2) : recommandation d'effectif calculée depuis
    # l'échéance la plus proche (dépôt de bagages compris). None tant que l'effort
    # de rotation n'est pas configuré. {level, recommended_cleaners, message, …}.
    signal: dict | None = None


class CalendarViewOut(BaseModel):
    """Charge complète de la vue « Séjours » : un seul appel pour tout rendre."""
    property_id: UUID
    default_checkin_time: time
    default_checkout_time: time
    calendars: list[CalendarOut] = []
    bookings: list[BookingOut] = []
    overlaps: list[OverlapOut] = []
    rotations: list[RotationOut] = []


class DeleteBookingOut(BaseModel):
    outcome: str          # 'deleted' (saisie directe) | 'cancelled' (importé)


# ── Catalogue de demandes & demandes par séjour (V2-23b, §1.2) ───────────────

_RequestOrigin = Literal["owner", "guest"]
_RequestStatus = Literal["pending", "accepted", "declined"]


class RequestTypeIn(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=120)
    sort_order: int = 0


class RequestTypeUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    sort_order: int | None = None
    is_active: bool | None = None


class RequestTypeOut(BaseModel):
    id: UUID
    code: str
    label: str
    sort_order: int
    is_active: bool


class BookingRequestIn(BaseModel):
    """Demande créée par le propriétaire (origin='owner'). L'origine 'guest' est
    posée côté serveur (volet 3), jamais par ce payload."""
    request_type_id: UUID | None = None
    label: str | None = Field(default=None, max_length=120)
    quantity: int = Field(default=1, ge=1, le=50)
    note: str | None = Field(default=None, max_length=500)
    status: _RequestStatus = "accepted"   # le propriétaire prépare → accepté d'emblée


class BookingRequestUpdate(BaseModel):
    quantity: int | None = Field(default=None, ge=1, le=50)
    note: str | None = Field(default=None, max_length=500)
    status: _RequestStatus | None = None


class BookingRequestOut(BaseModel):
    id: UUID
    booking_id: UUID
    request_type_id: UUID | None = None
    label: str | None = None
    quantity: int
    note: str | None = None
    origin: str
    status: str


class GuestServiceRequestIn(BaseModel):
    """Demande de service depuis le guide voyageur (V2-23b, §3.1). Le voyageur
    n'est pas authentifié : il ne choisit qu'une **section** offrant le service
    (le libellé stocké vient du template, jamais d'une valeur libre) et un message
    facultatif. Le rattachement au séjour et l'origine 'guest' sont posés serveur."""
    section: str = Field(min_length=1, max_length=60)   # code de la section requestable
    note: str | None = Field(default=None, max_length=500)
    # V2-23c volet 1bis : plus de `stay_token` dans le corps. Le rattachement
    # CERTAIN au séjour passe par la ROUTE `POST /b/{stay_token}/requests` (le
    # token de l'URL désigne le séjour côté serveur) ; le lien maison utilise
    # `POST /g/{guide_token}/requests` (rattachement deviné « en cours → suivant »).


class GuestServiceRequestOut(BaseModel):
    """Accusé de réception d'une demande du voyageur — jamais de détail de séjour."""
    ok: bool = True
    label: str
    message: str


class InterventionOut(BaseModel):
    """Une intervention calculée pour un séjour (§1.3) — jamais stockée."""
    kind: str
    on: date
    label: str
    tasks: list[str] = []
    needs_appointment: bool = False


class ShareLinkOut(BaseModel):
    """Lien de partage généré à la demande (V2-23c, volet 3) : lien de séjour
    (`/b/{stay_token}`) ou lien vitrine (`/v/{showcase_token}`). Le token est
    généré au premier usage puis réutilisé (idempotent) ; il n'est **jamais** créé
    ni renvoyé par une route publique (endpoints propriétaire authentifiés)."""
    token: str
    url: str


class SendTemplatesOut(BaseModel):
    """Gabarits courts d'envoi du guide (email/WhatsApp) résolus dans une langue
    (V2-23c, volet 3). Textes portés par l'inventaire i18n (clés `ui.send_*`,
    FR/EN/ES en code, langues supplémentaires via `ui_translations`). La fenêtre
    d'envoi compose le `mailto:`/`wa.me` côté client en substituant `{name}`,
    `{property}` et l'URL."""
    subject: str
    hello: str
    hello_generic: str
    intro: str
    signoff: str
