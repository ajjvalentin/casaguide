"""Schémas Pydantic (validation des requêtes / forme des réponses)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

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


class OwnerOut(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    company_name: str | None = None
    phone: str | None = None
    locale: str
    plan_id: str | None = None


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

class SecretsIn(BaseModel):
    wifi_ssid: str | None = None
    wifi_pass: str | None = None      # sera chiffré avant stockage
    keybox_code: str | None = None    # sera chiffré avant stockage
    keybox_notes: str | None = None


class SecretsOut(BaseModel):
    """Renvoyé uniquement au propriétaire authentifié (jamais au voyageur)."""
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
