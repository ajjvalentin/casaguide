"""Mémoire de secteur — fraîcheur, version, fusion, traçabilité (V2-78).

POURQUOI. Les passes web pèsent ~150-170 ct sur ~200 ct de coût marginal par guide
(mesuré sur `api_costs`, 3 jours : reputed_sorties 47,8 ct en moyenne, rental_web 21,
activities 28, markets 21, babysitter 17, service_rules 12 ; la traduction 0,9 ct et le
juge 2 ct sont négligeables). Elles tournaient à neuf pour chaque guide, même dans un
secteur déjà exploré la veille.

LA PRIORITÉ N'EST PAS L'ÉCONOMIE. « La pertinence d'une information à jour prime sur
l'économie : une donnée périmée est un guide qui trompe » (André, 18/09). Ce module
n'est donc PAS un cache : c'est une mémoire **datée et versionnée**, qu'on ne consomme
que si l'on peut répondre OUI aux deux questions — est-elle assez RÉCENTE pour ce type
d'information ? a-t-elle été écrite par le PROMPT D'AUJOURD'HUI ?

LES DEUX QUESTIONS, ET POURQUOI LES DEUX.

1. **Fraîcheur, par NATURE de l'information.** Un commerce ferme, un loueur change de
   numéro, un bar à chicha ouvre : 21 jours. Une plage, un sentier, un site historique
   ne bougent pas : 90 jours. Le délai est un réglage, jamais une valeur en dur.

2. **Version de contenu — leçon payée DEUX FOIS.** Une mémoire récente mais écrite par
   un prompt PÉRIMÉ est pire qu'une absence de mémoire : elle rend tout correctif de
   prompt INVISIBLE, puisque l'appel n'a plus lieu. C'est arrivé aux activités (V2-73b :
   positions jamais tentées, l'étape sautée parce que le fait était « frais ») PUIS à la
   chicha d'Adeje (V2-77f : prompt élargi sans effet, contacts absents, « 3 candidats sur
   6 » — on corrigeait le prompt et rien ne changeait). Toute passe estampille donc sa
   version ; un bump invalide la mémoire du secteur. **Une passe sans version n'a pas le
   droit à la mémoire** (`memory_decision` l'exige).

L'INVARIANT (acquis V2-56c, généralisé ici) : **la mémoire s'ENRICHIT, ne se VIDE pas.**
Une collecte fraîche qui rend moins que ce qu'on savait ne doit jamais faire disparaître
ce qu'on savait — le web est capricieux, un guide ne doit pas maigrir parce qu'une
recherche a moins bien répondu ce jour-là. `merge_items` fusionne par identité de nom, le
FRAIS gagnant sur le champ (il est plus à jour), l'ancien survivant s'il n'est plus rendu.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryDecision:
    """Verdict sur la mémoire d'un secteur pour une passe donnée. PUR."""

    use: bool                      # consommer la mémoire (aucun appel web)
    age_days: int | None           # âge de la mémoire, None si aucune
    reason: str                    # 'memory' | 'absent' | 'stale' | 'version' | 'forced'
    content: dict | None = None    # contenu mémorisé (même quand on re-collecte : fusion)

    def step_note(self, cost_cts: float | None = None) -> dict:
        """Traçabilité (V2-78 point 4) : ce que `steps` doit dire de cette passe, pour que
        le coût réel d'un guide soit LISIBLE — « mémoire (âge 3 j) » ou « collecte
        fraîche (12,4 ct) »."""
        if self.use:
            return {"source": "memory", "age_days": self.age_days}
        note: dict = {"source": "fresh", "why": self.reason}
        if self.age_days is not None:
            note["previous_age_days"] = self.age_days
        if cost_cts is not None:
            note["cost_cts"] = round(cost_cts, 2)
        return note


def memory_decision(fact: dict | None, age_days: int | None, *, max_age_days: int,
                    schema_v: int, refresh: bool = False) -> MemoryDecision:
    """Décide si la mémoire du secteur est CONSOMMABLE. PURE (aucune E/S) — le fait et son
    âge sont lus par l'appelant, la règle vit ici et NULLE PART AILLEURS.

    Refuse dans quatre cas, et le dit (`reason`, repris dans `steps`) :
      · `absent`  — rien en mémoire ;
      · `forced`  — `--refresh-sector` (recette, ou signalement d'une info périmée) ;
      · `stale`   — plus vieille que le délai propre à cette nature d'information ;
      · `version` — écrite par un schéma/prompt périmé (le piège V2-73b / V2-77f).

    Dans TOUS les cas de refus, `content` porte quand même la mémoire existante : la
    collecte fraîche doit FUSIONNER avec elle, jamais l'écraser (invariant V2-56c).
    """
    if fact is None:
        return MemoryDecision(False, None, "absent")
    if refresh:
        return MemoryDecision(False, age_days, "forced", fact)
    if fact.get("v") != schema_v:
        return MemoryDecision(False, age_days, "version", fact)
    if age_days is None or age_days > max_age_days:
        return MemoryDecision(False, age_days, "stale", fact)
    return MemoryDecision(True, age_days, "memory", fact)


def _key(name: str | None) -> str:
    """Identité d'un lieu pour la fusion : nom sans accents, minuscule, alphanumérique.
    Assez souple pour reconnaître « Café de Roberte » et « Cafe de Roberte », assez strict
    pour ne pas confondre deux établissements distincts."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return " ".join("".join(c if c.isalnum() else " " for c in s.lower()).split())


def merge_items(old: list | None, fresh: list | None) -> list:
    """Fusionne la mémoire et une collecte fraîche — INVARIANT V2-78/V2-56c : **la mémoire
    s'enrichit, ne se vide pas**.

    Le FRAIS gagne sur le champ (il est plus à jour) ; ce que la collecte du jour n'a pas
    rendu SURVIT (le web est capricieux — un guide ne doit pas maigrir parce qu'une
    recherche a moins bien répondu). L'ordre est déterministe : les frais d'abord, dans
    leur ordre, puis les rescapés de la mémoire dans le leur → idempotent.
    """
    fresh = [it for it in (fresh or []) if isinstance(it, dict)]
    old = [it for it in (old or []) if isinstance(it, dict)]
    seen = {_key(it.get("name")) for it in fresh if _key(it.get("name"))}
    survivors = [it for it in old
                 if _key(it.get("name")) and _key(it.get("name")) not in seen]
    return fresh + survivors
