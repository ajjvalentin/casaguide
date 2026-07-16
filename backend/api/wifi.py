"""Réseaux wifi multiples chiffrés (M-15, §8, invariant 5).

Un logement peut exposer plusieurs réseaux (Maison, Terrasse, Étage…). La liste
`[{label, ssid, pass}]` est **sérialisée en JSON puis chiffrée en un seul bytea**
via l'AES applicatif (`crypto`), stockée dans `property_secrets.wifi_networks_enc`.
La clé vit hors base : aucun mot de passe n'est jamais en clair côté serveur ni
dans `/data` (invariant 5).

Rétrocompatibilité / migration lazy (M-15) : tant que `wifi_networks_enc` est
NULL, `networks_from_row` synthétise le **réseau n°1** (label « Wifi ») à partir
des colonnes legacy `wifi_ssid` / `wifi_pass_enc` — l'ancien wifi n'a donc rien à
re-saisir. La première sauvegarde écrit `wifi_networks_enc` et laisse les colonnes
legacy en miroir du réseau n°1 (voir `routers/properties.set_secrets`).
"""
from __future__ import annotations

import json
from typing import Any

from . import crypto

DEFAULT_LABEL = "Wifi"


def _clean(net: Any) -> dict | None:
    """Normalise un réseau {label, ssid, pass}. None si vide (ni ssid ni pass)."""
    if not isinstance(net, dict):
        return None
    label = (net.get("label") or "").strip() or DEFAULT_LABEL
    ssid = (net.get("ssid") or "").strip() or None
    pw = net.get("pass")
    pw = pw.strip() if isinstance(pw, str) else None
    pw = pw or None
    if not ssid and not pw:
        return None  # réseau vide → ignoré (jamais stocké)
    return {"label": label, "ssid": ssid, "pass": pw}


def clean_networks(networks: Any) -> list[dict]:
    """Liste normalisée de réseaux (vides écartés), dans l'ordre fourni."""
    return [c for n in (networks or []) if (c := _clean(n))]


def encrypt_networks(networks: Any) -> bytes | None:
    """Chiffre la liste de réseaux en un seul bytea. None si aucune entrée utile."""
    cleaned = clean_networks(networks)
    if not cleaned:
        return None
    return crypto.encrypt(json.dumps(cleaned, ensure_ascii=False))


def networks_from_row(row: dict | None) -> list[dict]:
    """Liste **déchiffrée** des réseaux d'une ligne `property_secrets`.

    Repli legacy (M-15) : si `wifi_networks_enc` est absent mais que les colonnes
    historiques portent un wifi, l'ancien réseau devient le réseau n°1 (« Wifi »).
    Retourne `[{label, ssid, pass}]` (pass en clair, réservé aux appelants
    autorisés — propriétaire ou voyageur en mode 'link')."""
    if not row:
        return []
    blob = row.get("wifi_networks_enc")
    if blob is not None:
        data = crypto.decrypt(blob)
        try:
            parsed = json.loads(data) if data else []
        except (ValueError, TypeError):
            parsed = []
        return clean_networks(parsed)
    # Repli : ancien wifi unique → réseau n°1
    ssid = row.get("wifi_ssid")
    pw = crypto.decrypt(row.get("wifi_pass_enc")) if row.get("wifi_pass_enc") else None
    net = _clean({"label": DEFAULT_LABEL, "ssid": ssid, "pass": pw})
    return [net] if net else []


def first_network(networks: list[dict]) -> dict | None:
    """Réseau n°1 (pour alimenter les colonnes legacy / les champs de compat)."""
    return networks[0] if networks else None
