"""Dédoublonnage à la suggestion (V2-40) — passe PURE et testable.

Constat (18/08, écran réel) : OSM porte souvent le MÊME lieu en plusieurs éléments
(Aeropuerto de Murcia « (RMU) » + son nom officiel long ; Alicante-Elche « (ALC) »
et « Miguel Hernández » = deux points de géométrie du même aéroport ; une gare en
castillan + valencien). Le dédoublonnage historique ne comparait que `source_ref` ;
le trigramme des marchés (volet 3) n'avait jamais été généralisé — c'est ici.

La passe s'intercale ENTRE la moisson Overpass (distances comprises) et l'upsert,
**par catégorie** (des catégories différentes à 10 m ne fusionnent jamais). Deux
règles, aucune écriture (pure) :

  1. `deduplicate` — dans le lot d'une catégorie, deux candidats sont doublons si
     leurs noms se ressemblent (trigrammes) OU s'ils sont à moins de ~150 m. Le
     SURVIVANT est le mieux renseigné (score de champs), départage : temps de trajet
     le plus court (le point de géométrie le plus juste, cas Alicante 52 vs 72), puis
     nom le plus court.

  2. `filter_against_existing` — un candidat qui double une fiche déjà ARBITRÉE est
     retiré : une fiche RETENUE (approved/edited) sur nom OU distance (ne pas
     reproposer au propriétaire son aéroport sous un autre `source_ref`) ; une fiche
     REJETÉE sur le NOM SEULEMENT, jamais la distance (la variante valencienne d'une
     gare rejetée disparaît ; mais un successeur légitime au même endroit — XiaoWu à
     l'adresse du Bon Bon rejeté — VIT, car son nom diffère). Les fiches arbitrées
     elles-mêmes ne sont jamais modifiées : le ménage du passé reste au propriétaire.

Calibrage des seuils (mesuré sur les cas réels — voir rapport V2-40) :
`_dice` (Dice sur trigrammes) séparait nettement les VARIANTES du même lieu des
lieux distincts : Alicante « (ALC) » vs « Miguel Hernández » ≈ 0,77, gare bilingue
≈ 0,88 ; deux aéroports distincts ≈ 0,56, deux pharmacies ≈ 0,50, suffixe de zone
« (Aguamarina) » de deux agences distinctes ≈ 0,36. Le seuil 0,70 les tranche avec
marge. 150 m = deux points au même lieu (précédent marchés : `_MARKET_SAME_SPOT_M`
= 250 m pour des places de marché plus larges ; ici des points de service).
"""
from __future__ import annotations

import re
import unicodedata

from .overpass import haversine_m

# ── Seuils calibrés (V2-40) ──────────────────────────────────────────────────
NAME_SIM_THRESHOLD = 0.70   # Dice trigrammes ∈ [0,1] : au-delà = même lieu
DUP_DIST_M = 150.0          # deux points à moins de 150 m = même lieu

# Champs dont la présence fait le « mieux renseigné » (survivant d'un doublon).
_SCORE_FIELDS = ("phone", "website", "opening_hours", "cuisine", "locality")


def _norm(s: str | None) -> str:
    """Nom normalisé pour comparaison : sans accents, minuscule, ponctuation → espace,
    espaces compactés. AUCUN mot générique retiré (générique à toutes catégories)."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return " ".join(s.split())


def _noparen(s: str | None) -> str:
    """Nom sans le contenu entre parenthèses, puis normalisé — attrape « (ALC) » vs
    « Miguel Hernández » par leur préfixe commun (précédent Aguamarina : deux agences
    distinctes gardent des bases différentes → jamais fusionnées)."""
    return _norm(re.sub(r"\([^)]*\)", " ", s or ""))


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s}


def _dice(a: str, b: str) -> float:
    """Coefficient de Dice sur trigrammes ∈ [0,1] (tolérant aux variantes)."""
    if not a or not b:
        return 1.0 if a == b else 0.0
    ta, tb = _trigrams(a), _trigrams(b)
    return 2 * len(ta & tb) / (len(ta) + len(tb)) if (ta or tb) else 0.0


def name_similar(a: str | None, b: str | None) -> bool:
    """Deux noms désignent-ils le même lieu ? Dice ≥ seuil sur le nom complet OU sur
    le nom sans parenthèses (le max des deux)."""
    return max(_dice(_norm(a), _norm(b)),
               _dice(_noparen(a), _noparen(b))) >= NAME_SIM_THRESHOLD


def _distance_m(a: dict, b: dict) -> float | None:
    if a.get("lat") is None or a.get("lon") is None:
        return None
    if b.get("lat") is None or b.get("lon") is None:
        return None
    return float(haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]))


def _same_place(a: dict, b: dict) -> bool:
    """Même lieu : nom similaire OU distance ≤ seuil. Par catégorie seulement — si les
    deux portent un `category` différent, jamais fusionnés (pharmacie ≠ mall à 10 m)."""
    if a.get("category") and b.get("category") and a["category"] != b["category"]:
        return False
    if name_similar(a.get("name"), b.get("name")):
        return True
    d = _distance_m(a, b)
    return d is not None and d <= DUP_DIST_M


def _nonempty(v) -> bool:
    return bool(v.strip()) if isinstance(v, str) else v is not None


def _score(p: dict) -> int:
    """Nombre de champs renseignés (tél, site, horaires, cuisine, locality)."""
    return sum(1 for f in _SCORE_FIELDS if _nonempty(p.get(f)))


def _travel(p: dict) -> float:
    """Temps de trajet du candidat (le plus court des modes disponibles) — départage
    deux points de géométrie du même lieu (le plus proche = le plus juste, cas
    Alicante 52 vs 72). Repli sur la distance à vol d'oiseau si les temps manquent."""
    times = [t for t in (p.get("drive_min"), p.get("walk_min")) if t is not None]
    if times:
        return float(min(times))
    return float(p.get("crow_m") if p.get("crow_m") is not None else float("inf"))


def _better(a: dict, b: dict) -> dict:
    """Le survivant entre deux doublons : plus de champs remplis ; à égalité, temps de
    trajet le plus court ; à égalité encore, nom le plus court. Déterministe : à
    stricte égalité finale, garde `a` (le premier rencontré → ordre stable)."""
    sa, sb = _score(a), _score(b)
    if sa != sb:
        return a if sa > sb else b
    ta, tb = _travel(a), _travel(b)
    if ta != tb:
        return a if ta < tb else b
    return b if len(b.get("name") or "") < len(a.get("name") or "") else a


def deduplicate(candidates: list[dict]) -> tuple[list[dict], int]:
    """Dédoublonne un lot de candidats d'UNE catégorie (moisson + distances). Renvoie
    `(survivants, n_fusionnés)`. Ordre stable ; garde le mieux renseigné à sa place."""
    survivors: list[dict] = []
    merged = 0
    for c in candidates:
        idx = next((i for i, s in enumerate(survivors) if _same_place(c, s)), None)
        if idx is None:
            survivors.append(c)
        else:
            merged += 1
            survivors[idx] = _better(survivors[idx], c)
    return survivors, merged


def filter_against_existing(candidates: list[dict],
                            existing: list[dict]) -> tuple[list[dict], int]:
    """Retire les candidats qui doublent une fiche déjà ARBITRÉE de la même catégorie.

    `existing` : POI approved/edited/rejected [{name, lat, lon, status}]. Retenue →
    nom OU distance ; rejetée → NOM SEULEMENT (jamais la distance). Renvoie
    `(gardés, n_retirés)`. Ne modifie jamais `existing`."""
    kept: list[dict] = []
    removed = 0
    for c in candidates:
        drop = False
        for ex in existing:
            # La MÊME fiche re-moissonnée (source_ref identique) n'est PAS un doublon
            # « sous un autre source_ref » : elle passe par l'upsert (idempotent,
            # respectueux du statut ; c'est lui qui, ex., complète une localité NULL
            # d'une fiche retenue — V2-38bis). Ne jamais la retirer ici.
            if (c.get("source_ref") and ex.get("source_ref")
                    and c["source_ref"] == ex["source_ref"]):
                continue
            if ex.get("status") == "rejected":
                if name_similar(c.get("name"), ex.get("name")):
                    drop = True
                    break
            elif _same_place(c, ex):     # retenue : nom OU distance
                drop = True
                break
        if drop:
            removed += 1
        else:
            kept.append(c)
    return kept, removed
