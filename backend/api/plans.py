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
                  addon_qty, staff_grandfathered, created_at, updated_at
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
    """Droits effectifs d'un propriétaire : plan courant + état d'essai + add-ons.

    **Point d'entrée unique** des contrôles de droits (V2-18a) : `check_quota`,
    le garde de lecture seule, le watermark et le gating staff s'y branchent.
    Renvoie toujours un plan (jamais None, cf. `get_plan`).

    Clés : `plan` (dict), `on_trial` (essai en cours), `trial_expired` (essai
    échu → lecture seule), `read_only` (alias métier de trial_expired),
    `trial_ends_at` (échéance ou None), `addon_qty` (logements supplémentaires
    Stripe, V2-18b), `max_properties` (plafond EFFECTIF = base + add-ons, None si
    illimité), `staff_grandfathered` (clause de grand-père), `staff_access`
    (droit au guide équipe /s/)."""
    sub = get_subscription(conn, owner_id)
    plan = get_plan(conn, owner_id)
    ends = sub.get("trial_ends_at") if sub else None
    trialing = bool(sub and sub.get("status") == "trialing" and ends is not None)
    expired = is_trial_expired(sub, now=now)
    addon_qty = int(sub.get("addon_qty") or 0) if sub else 0
    grandfathered = bool(sub.get("staff_grandfathered")) if sub else False
    base_props = plan.get("max_properties")
    max_props = None if base_props is None else base_props + addon_qty
    return {
        "plan": plan,
        "on_trial": trialing and not expired,
        "trial_expired": expired,
        "read_only": expired,
        "trial_ends_at": ends,
        "addon_qty": addon_qty,
        "max_properties": max_props,
        "staff_grandfathered": grandfathered,
        "staff_access": staff_access(plan, grandfathered),
    }


# Statuts d'un abonnement Stripe qui laissent l'accès payant ouvert (V2-18d) :
# 'active' et 'past_due' (grâce pendant les relances). 'canceled'/'trialing' non.
_PAID_ACTIVE_STATUSES = ("active", "past_due")


def has_active_paid_subscription(conn, owner_id: str) -> dict | None:
    """Abonnement Stripe **payant et actif** du propriétaire, ou None (V2-18d).

    « Payant actif » = plan à prix > 0, statut 'active'/'past_due', ET un
    `stripe_subscription_id` réel (donc modifiable côté Stripe). Sert à la fois à
    interdire un second Checkout (409 `already_subscribed`) et à autoriser un
    changement d'offre in-place (`/billing/change-plan`). L'essai ('trialing') et
    le gratuit ne comptent pas."""
    sub = get_subscription(conn, owner_id)
    if not sub or sub.get("status") not in _PAID_ACTIVE_STATUSES:
        return None
    if not sub.get("stripe_subscription_id"):
        return None
    plan = get_plan(conn, owner_id)
    if plan.get("price_month_cts", 0) <= 0:
        return None
    return sub


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
    (essai/gratuit/solo → oui ; pro → non — échelle de marque, V2-18a)."""
    return has_feature(plan, "watermark")


def staff_access(plan: dict, grandfathered: bool = False) -> bool:
    """Le guide équipe (/s/) est-il accessible pour ce plan (V2-18b) ?

    Exclusif Pro (`features.staff_guide == true`), avec aperçu pendant l'essai
    (`'preview'`). Les comptes existants à la migration en gardent l'accès quel
    que soit leur plan (`grandfathered`, invariant 3). Solo/gratuit fermés."""
    flag = _features(plan).get("staff_guide")
    return bool(grandfathered or flag is True or flag == "preview")


# Sentinelle « toutes les langues » (grille V2-18b : plafond supprimé sur tous
# les plans). En base, `features.langs == 'all'` ; côté code → illimité (None).
LANGS_UNLIMITED = "all"


def max_langs(plan: dict) -> int | None:
    """Nombre total de langues publiables (langue source comprise), ou **None =
    illimité** (grille V2-18b : `features.langs == 'all'`). Défaut : 1 (langue
    source seule) si le plan ne précise rien."""
    v = _features(plan).get("langs", 1)
    if v == LANGS_UNLIMITED or v is None:
        return None
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 1


def cap_target_langs(plan: dict, targets: list[str]) -> list[str]:
    """Restreint la liste des langues **cibles** de traduction au plafond du plan.

    La langue source compte dans `features.langs` : un plan à `langs=1` n'autorise
    **aucune** cible ; `langs=5` en autorise 4 ; `langs='all'` (illimité) les
    autorise toutes. On coupe la liste (ordre d'entrée préservé) sans jamais lever
    d'erreur."""
    limit = max_langs(plan)
    if limit is None:                       # illimité : toutes les cibles
        return list(targets)
    allowed = max(0, limit - 1)
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
    ent = effective_entitlements(conn, owner_id)
    plan = ent["plan"]

    if resource == "properties":
        used = repo.count_properties(conn, owner_id)
        limit = ent["max_properties"]        # base + add-ons (None = illimité)
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
        limit = max_langs(plan)              # None = illimité (grille V2-18b)
        used = 1  # langue source, toujours publiée
        if property_id is not None:
            published = repo.published_langs(conn, property_id)
            used = 1 + len([l for l in published if l])
        ok = limit is None or used <= limit
        return QuotaResult(ok=ok, used=used, limit=limit, plan=plan)

    raise ValueError(f"ressource de quota inconnue : {resource!r}")
