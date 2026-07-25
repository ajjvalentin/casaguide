"""Couche d'accès aux plans & abonnements (V2-05a, CdC §10).

Le modèle de données (`plans` + `subscriptions`) est la **source unique de
vérité** des quotas : aucune limite n'est codée en dur ici. Ce module lit ce
modèle et le traduit en décisions exploitables par les routers (création de
logement, enrichissement, traductions, marque blanche).

Invariants de la mission :
  1. Un downgrade ne supprime jamais de données — les quotas ne bornent que la
     *création* et la *publication de nouvelles* traductions, jamais l'existant.
  2. Les quotas sont appliqués côté serveur ; les routers refusent proprement en
     402 `quota_exceeded`. Ce module ne lève pas d'HTTPException (découplage) :
     il renvoie un `QuotaResult` que le router traduit en réponse.
  3. La définition des plans vit en base ; `check_quota` lit `max_properties`,
     `enrich_quota` (mensuel, par logement) et `features` (langs, watermark…).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from . import repo

log = logging.getLogger("casaguide.api")

# Plan de repli si un compte n'a AUCUN abonnement (ne devrait plus arriver après
# la migration 007, mais on ne fait jamais confiance aveugle à l'état de la base).
FALLBACK_PLAN_ID = "free"


@dataclass(frozen=True)
class QuotaResult:
    """Décision de quota pour une ressource donnée.

    `limit is None` ⇒ ressource illimitée (`ok` toujours vrai). `used`/`limit`
    servent aussi aux jauges du back-office (page « Mon abonnement »)."""
    ok: bool
    used: int
    limit: int | None      # None = illimité
    plan: dict

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)


def get_subscription(conn, owner_id: str) -> dict | None:
    """Abonnement courant (ligne la plus récente) du propriétaire, ou None."""
    return conn.execute(
        """SELECT id, owner_id, plan_id, status, stripe_customer_id,
                  stripe_subscription_id, trial_ends_at, current_period_end,
                  created_at, updated_at
           FROM subscriptions
           WHERE owner_id = %s
           ORDER BY created_at DESC LIMIT 1""",
        (owner_id,),
    ).fetchone()


def get_plan(conn, owner_id: str) -> dict:
    """Plan courant du propriétaire (jointure `plans`). Ne renvoie **jamais**
    None : à défaut d'abonnement (état incohérent), repli sur le plan gratuit
    avec un avertissement journalisé — on n'ouvre jamais un accès illimité par
    accident."""
    plan = repo.get_owner_plan(conn, owner_id)
    if plan is not None:
        return plan
    log.warning(
        "Aucun abonnement pour owner_id=%s : repli sur le plan '%s'. "
        "La migration 007 aurait dû créer une ligne — vérifier la base.",
        owner_id, FALLBACK_PLAN_ID)
    fallback = repo.get_plan_by_id(conn, FALLBACK_PLAN_ID)
    if fallback is None:  # base sans seed : dernier repli, tout est verrouillé
        return {"id": FALLBACK_PLAN_ID, "name": "Gratuit", "max_properties": 1,
                "enrich_quota": 1, "price_month_cts": 0,
                "features": {"langs": 1, "watermark": True}}
    return fallback


# ── Essai & droits effectifs (V2-18a) ────────────────────────────────────────

TRIAL_PLAN_ID = "trial"
TRIAL_DAYS = 21


def _now(now: datetime | None) -> datetime:
    """Horloge injectable (tests). Toujours en UTC aware."""
    return now if now is not None else datetime.now(timezone.utc)


def is_trial_expired(sub: dict | None, *, now: datetime | None = None) -> bool:
    """Vrai si l'abonnement est un essai arrivé à échéance.

    L'expiration est calculée À LA LECTURE (invariant V2-18a) : un essai est
    « expiré » ssi son statut est 'trialing' ET `trial_ends_at` est dépassé.
    Jamais de job qui basculerait des lignes — pas d'état à maintenir, pas de
    course. Un abonnement payant / gratuit / sans échéance n'est jamais expiré."""
    if not sub or sub.get("status") != "trialing":
        return False
    ends = sub.get("trial_ends_at")
    if ends is None:
        return False
    if ends.tzinfo is None:  # défensif : normalise en UTC aware
        ends = ends.replace(tzinfo=timezone.utc)
    return ends < _now(now)


def effective_entitlements(conn, owner_id: str, *,
                           now: datetime | None = None) -> dict:
    """Droits effectifs d'un propriétaire : plan courant + état d'essai.

    **Point d'entrée unique** des contrôles de droits (V2-18a) : `check_quota`,
    le garde de lecture seule et le watermark s'y branchent. Renvoie toujours un
    plan (jamais None, cf. `get_plan`).

    Clés : `plan` (dict), `on_trial` (essai en cours), `trial_expired` (essai
    échu → lecture seule), `read_only` (alias métier de trial_expired),
    `trial_ends_at` (échéance ou None)."""
    sub = get_subscription(conn, owner_id)
    plan = get_plan(conn, owner_id)
    ends = sub.get("trial_ends_at") if sub else None
    trialing = bool(sub and sub.get("status") == "trialing" and ends is not None)
    expired = is_trial_expired(sub, now=now)
    return {
        "plan": plan,
        "on_trial": trialing and not expired,
        "trial_expired": expired,
        "read_only": expired,
        "trial_ends_at": ends,
    }


def can_write(conn, owner_id: str, *, now: datetime | None = None) -> bool:
    """Le propriétaire peut-il écrire (créer/éditer) ? Faux si l'essai est expiré
    (back-office en lecture seule, V2-18a). Les lectures restent toujours servies
    et aucune donnée n'est jamais supprimée (invariant 1)."""
    return not effective_entitlements(conn, owner_id, now=now)["trial_expired"]


# ── Fonctionnalités (features JSONB) ─────────────────────────────────────────

def _features(plan: dict) -> dict:
    feats = plan.get("features")
    return feats if isinstance(feats, dict) else {}


def has_feature(plan: dict, name: str) -> bool:
    """Vrai si le drapeau `features.<name>` est activé pour ce plan."""
    return bool(_features(plan).get(name))


def wants_watermark(plan: dict) -> bool:
    """Le guide voyageur doit-il porter le pied de page « Créé avec Holaguia » ?
    (plan gratuit → oui ; plans payants → non)."""
    return has_feature(plan, "watermark")


def max_langs(plan: dict) -> int:
    """Nombre total de langues publiables (langue source comprise). Défaut : 1
    (langue source seule) si le plan ne précise rien."""
    try:
        return max(1, int(_features(plan).get("langs", 1)))
    except (TypeError, ValueError):
        return 1


def cap_target_langs(plan: dict, targets: list[str]) -> list[str]:
    """Restreint la liste des langues **cibles** de traduction au plafond du plan.

    La langue source compte dans `features.langs` : un plan à `langs=1` (gratuit)
    n'autorise donc **aucune** langue cible ; `langs=5` en autorise 4. On coupe la
    liste (ordre d'entrée préservé) sans jamais lever d'erreur."""
    allowed = max(0, max_langs(plan) - 1)
    return list(targets[:allowed])


# ── Quotas ───────────────────────────────────────────────────────────────────

def check_quota(conn, owner_id: str, resource: str, *,
                property_id: str | None = None) -> QuotaResult:
    """Évalue un quota pour `resource` ∈ {'properties', 'enrichments', 'langs'}.

    - `properties`   : nombre de logements vs `max_properties` (NULL = illimité).
    - `enrichments`  : jobs d'enrichissement du mois calendaire **pour ce
      logement** (`property_id` requis) vs `enrich_quota`.
    - `langs`        : nombre de langues déjà publiées (source comprise) vs
      `features.langs` — informatif (jauges) ; le plafonnement effectif des
      traductions se fait via `cap_target_langs`.

    Le plan est résolu via `effective_entitlements` (point d'entrée unique des
    droits, V2-18a). NB : à l'expiration de l'essai, la création est de toute
    façon refusée en amont par le garde de lecture seule (403 `trial_expired`) —
    ce module ne connaît pas FastAPI et ne lève pas d'exception.
    """
    plan = effective_entitlements(conn, owner_id)["plan"]

    if resource == "properties":
        used = repo.count_properties(conn, owner_id)
        limit = plan.get("max_properties")   # None = illimité
        ok = limit is None or used < limit
        return QuotaResult(ok=ok, used=used, limit=limit, plan=plan)

    if resource == "enrichments":
        if property_id is None:
            raise ValueError("check_quota('enrichments') exige property_id")
        used = repo.count_jobs_current_month(conn, property_id)
        limit = plan.get("enrich_quota")
        ok = limit is None or used < limit
        return QuotaResult(ok=ok, used=used, limit=limit, plan=plan)

    if resource == "langs":
        limit = max_langs(plan)
        used = 1  # langue source, toujours publiée
        if property_id is not None:
            published = repo.published_langs(conn, property_id)
            used = 1 + len([l for l in published if l])
        ok = used <= limit
        return QuotaResult(ok=ok, used=used, limit=limit, plan=plan)

    raise ValueError(f"ressource de quota inconnue : {resource!r}")
