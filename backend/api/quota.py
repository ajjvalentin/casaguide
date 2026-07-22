"""Refus de quota HTTP normalisé (V2-05a, volet 2).

Point unique pour renvoyer un dépassement de quota côté API : **HTTP 402
Payment Required** avec un `detail` structuré `{"code": "quota_exceeded",
"message": <FR>}`. Le front intercepte `code == 'quota_exceeded'` pour afficher
un encart « passez à une offre supérieure » (jamais un `alert()` brut).

Séparé de `api/plans.py` (qui ne connaît pas FastAPI) : la couche d'accès décide
*si* le quota est dépassé, ce module traduit la décision en réponse HTTP.
"""
from __future__ import annotations

from fastapi import HTTPException, status

QUOTA_EXCEEDED = "quota_exceeded"


def quota_exceeded(message: str) -> HTTPException:
    """Construit l'exception 402 à lever depuis un router quand un quota du plan
    est atteint. `message` est un texte FR affichable tel quel à l'utilisateur."""
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={"code": QUOTA_EXCEEDED, "message": message},
    )
