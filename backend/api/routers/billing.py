"""Catalogue des plans & abonnement du propriétaire (V2-05a, volet 3).

Alimente l'UI back-office :
  - `GET /api/plans`        catalogue public (formulaire d'inscription — prix
                            lus depuis la base, jamais codés en dur).
  - `GET /api/subscription` plan courant + jauges d'utilisation (page « Mon
                            abonnement », propriétaire authentifié).

Aucune collecte de paiement ici (Stripe = V2-05b). Les boutons de changement de
plan côté front restent inactifs (« paiement disponible prochainement »).
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import plans, repo
from ..deps import Conn, CurrentOwner
from ..schemas import PlanOut, QuotaGaugeOut, SubscriptionOut, UsageOut

router = APIRouter(prefix="/api", tags=["billing"])


@router.get("/plans", response_model=list[PlanOut])
def list_plans(conn: Conn):
    """Catalogue public des offres (par prix croissant). Pas d'auth : le
    formulaire d'inscription l'affiche avant toute session."""
    return repo.list_plans(conn)


@router.get("/subscription", response_model=SubscriptionOut)
def my_subscription(conn: Conn, owner: CurrentOwner):
    """Plan courant du propriétaire + jauges d'utilisation. Les limites viennent
    du plan (base), jamais du code (invariant 8)."""
    owner_id = str(owner["id"])
    plan = plans.get_plan(conn, owner_id)
    sub = plans.get_subscription(conn, owner_id)
    feats = plan.get("features") or {}

    usage = UsageOut(
        properties=QuotaGaugeOut(
            used=repo.count_properties(conn, owner_id),
            limit=plan.get("max_properties")),
        enrichments=QuotaGaugeOut(
            used=repo.count_owner_jobs_current_month(conn, owner_id),
            limit=plan.get("enrich_quota")),
        langs=QuotaGaugeOut(
            # Langue source (toujours publiée) + le plus de cibles publiées sur
            # un des logements ; plafond = features.langs (source comprise).
            used=1 + repo.max_published_langs_count(conn, owner_id),
            limit=plans.max_langs(plan)),
    )
    return SubscriptionOut(
        plan=PlanOut(**plan),
        status=(sub["status"] if sub else "active"),
        usage=usage)
