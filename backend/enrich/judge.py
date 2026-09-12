"""Juge IA du flux POI — CŒUR PARTAGÉ (V2-54).

Extrait des fonctions PURES du benchmark hors-pipeline (V2-45, `ops/poi_judge_benchmark.py`)
pour les partager avec le pipeline d'enrichissement : l'offre « Guide Voyageur » (V2-54)
juge chaque POI moissonné et écarte d'office les rejets à confiance ≥ seuil (pas de triage
humain sur cette offre). Le benchmark ré-importe ces symboles (métriques/rapport restent
côté `ops`) → aucune duplication, une seule vérité du prompt et du parsing.

Ce module ne fait AUCUNE E/S : l'appel Claude est toujours INJECTÉ (`ask`), les métriques
et la comptabilité vivent chez l'appelant (benchmark ou pipeline).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("casaguide.judge")

_JUDGE_MAX_TOKENS = 4000            # sortie JSON d'un lot de verdicts


# ── Prompt du juge (le STATUT n'y figure JAMAIS) ─────────────────────────────

_CRITERIA = """\
Tu es un DÉTECTEUR DE BRUIT pour un guide d'accueil de logement de vacances, PAS un
éditeur qui sélectionne les meilleurs lieux. Ton seul rôle : écarter les fiches
manifestement parasites et laisser TOUT le reste. Le propriétaire veut un ANNUAIRE
d'options (redondances comprises), pas une liste minimale. Sur une moisson typique,
tu ne devrais rejeter QU'ENVIRON 20-25 % des lieux. Rejeter EXIGE un motif POSITIF de
bruit tiré de la liste ci-dessous ; en l'absence d'un tel motif, garde (`keep`). Le
DOUTE profite TOUJOURS au maintien — un `keep` ne détruit rien (le propriétaire tranche
ensuite), un `reject` à tort supprime une information utile.

REJETER (verdict `reject`) est légitime UNIQUEMENT pour l'un de ces motifs :
- NOM GÉNÉRIQUE là où un NOM PROPRE est ATTENDU (commerce, restaurant, site touristique,
  équipement de JEU anonyme) : « Speeltuintje », « Trampoline », « Ballenbad », « Aire de
  jeux » (mais « Trampoline Park Zeeland » est un nom propre → garder). La GÉNÉRICITÉ est
  RELATIVE à la catégorie : une INFRASTRUCTURE FONCTIONNELLE porte légitimement un nom
  fonctionnel — « Parada de Taxis », un arrêt de bus, une borne de recharge, un
  distributeur, une station-service ne sont JAMAIS du bruit pour cette seule raison ;
- ERREUR DE CATÉGORIE manifeste : un hôpital classé « gare routière », un cinéma classé
  « marché », une agence immobilière taggée « marché »… le lieu ne correspond pas à sa
  catégorie ;
- INFRASTRUCTURE NON CIVILE ou non ouverte au public (base militaire en « aéroport »,
  héliport privé) ;
- SURNUMÉRAIRE au-delà du raisonnable pour la catégorie : une 4e plateforme nationale de
  livraison/baby-sitting quand 3 suffisent ; un aéroport ou une gare SUPPLÉMENTAIRE
  au-delà des 1-2 PRINCIPAUX de la zone (un 3e aéroport lointain n'ajoute rien) ;
- DOUBLON LOINTAIN d'un équipement DE PROXIMITÉ — du QUOTIDIEN (catégorie C :
  supermarché, boulangerie, distributeur…) OU de loisir de proximité (aire de jeux, petit
  terrain de quartier) — alors qu'un équivalent PROCHE existe, ou simplement trop loin
  pour son usage de proximité (boulangerie à 37 min quand une autre est à 8 ; aire de jeux
  à 49 min).

NE JAMAIS rejeter pour l'un de ces motifs (ils ne sont PAS du bruit) :
- LA DISTANCE SEULE. Le guide cible des vacanciers MOTORISÉS ; en zone rurale, rouler
  est normal. Tolérances par famille (indicatives, jamais un couperet) :
    • quotidien (C : supermarché, boulangerie, marché, distributeur, poste, laverie,
      centre commercial) : large, jusqu'à ~20-25 min ;
    • santé / sécurité (D : hôpital, pharmacie, médecin, police, vétérinaire) : GARDE
      les alternatives jusqu'à ~40 min — la redondance DIRECTIONNELLE (un hôpital de
      chaque côté) est une valeur de sécurité, pas du bruit ;
    • DESTINATIONS de loisir/tourisme pour lesquelles on se DÉPLACE (plage, site
      touristique, parc d'attractions, grand parcours de golf) : AUCUN plafond de
      distance — une plage à 50 min, un grand site à 60 min sont des informations utiles ;
    • ÉQUIPEMENT de PROXIMITÉ de loisir (aire de jeux, petit terrain de sport de
      quartier) : traité COMME le quotidien — un tel équipement LOINTAIN (aire de jeux à
      49 min) est du bruit, pas une destination ;
    • TRANSPORTS LOURDS (aéroport, gare) : pas de plafond de distance NON PLUS, mais
      garde seulement les 1 ou 2 PRINCIPAUX de la zone — un aéroport (ou une gare)
      SUPPLÉMENTAIRE plus lointain, au-delà de ces majeurs, est surnuméraire (bruit) ;
- LA REDONDANCE en santé, sécurité ou carburant (2e/3e station-service, 2e dentiste,
  2e pharmacie…) : les options de secours sont VOULUES ;
- L'ABSENCE d'adresse, de description ou de site web : c'est une lacune de la SOURCE
  (OpenStreetMap), pas un défaut du lieu. Un « Shell », une « Nieuwe kerk », une borne
  de recharge sans fiche riche restent des lieux réels et pertinents ;
- UN A PRIORI d'inutilité de la CATÉGORIE (dentistes, bornes de recharge, vétérinaires…) :
  décider quelles catégories figurent au guide est une décision PRODUIT déjà prise, pas
  la tienne. Juge le lieu, jamais l'utilité de sa catégorie."""


# Familles de catégories du quotidien (C) : sert au signal rural/urbain (densité de
# la moisson) fourni au juge — miroir des chapitres du seed, pas une nouvelle vérité.
_EVERYDAY_CATEGORIES = frozenset({
    "supermarket", "bakery", "market", "atm", "post_office", "laundry", "mall"})


def zone_hint(pois: list[dict]) -> str:
    """Signal rural/urbain DÉDUIT DE LA DENSITÉ DE LA MOISSON (spec V2-45 1bis) : si le
    commerce du quotidien le plus proche est loin en voiture, la zone est rurale. Neutre
    (« indéterminée ») si l'information manque — jamais une affirmation gratuite."""
    times = [p["drive_min"] for p in pois
             if p.get("category_code") in _EVERYDAY_CATEGORIES
             and p.get("drive_min") is not None]
    if not times:
        return ("indéterminée (peu de commerces du quotidien moissonnés — probablement "
                "rurale)")
    nearest = min(times)
    if nearest >= 12:
        return (f"RURALE (le commerce du quotidien le plus proche est à ~{nearest} min "
                f"en voiture) — vacanciers motorisés, rouler est normal")
    if nearest <= 5:
        return f"urbaine ou périurbaine (commerces du quotidien à ~{nearest} min)"
    return f"semi-rurale (commerces du quotidien à ~{nearest} min)"


def build_prompt(prop: dict, batch: list[dict], zone_type: str | None = None) -> str:
    """Construit le prompt d'un lot. Contexte du logement + critères + la liste des
    lieux SANS leur statut. Demande un JSON strict `{"verdicts": [...]}`.
    `zone_type` : signal rural/urbain calculé sur TOUTE la moisson (voir `zone_hint`)."""
    zone = f'{prop.get("city") or "?"}'
    if prop.get("region"):
        zone += f', {prop["region"]}'
    zone += f' ({prop.get("country_code") or "?"})'
    coords = ""
    if prop.get("lat") is not None and prop.get("lon") is not None:
        coords = f' — coordonnées {prop["lat"]:.4f},{prop["lon"]:.4f}'
    lines = []
    for p in batch:
        dist = []
        if p.get("walk_min") is not None:
            dist.append(f'{p["walk_min"]} min à pied')
        if p.get("drive_min") is not None:
            dist.append(f'{p["drive_min"]} min en voiture')
        dist_txt = f' — {", ".join(dist)}' if dist else " — distance inconnue"
        loc = f' [{p["locality"]}]' if p.get("locality") else ""
        addr = f' — {p["address"]}' if p.get("address") else ""
        desc = (p.get("description_md") or "").strip().replace("\n", " ")
        desc_txt = f' — « {desc[:160]} »' if desc else ""
        lines.append(
            f'- id "{p["id"]}" : {p["name"]}{loc} (catégorie {p["category_code"]}, '
            f'source {p.get("source") or "?"}){addr}{dist_txt}{desc_txt}')
    poi_block = "\n".join(lines)
    zt = zone_type or "indéterminée"
    return (
        f"{_CRITERIA}\n\n"
        f"LOGEMENT : {prop.get('name') or 'logement'} à {zone}{coords}.\n"
        f"Type : location de vacances. ZONE : {zt}. Le guide cible des vacanciers "
        f"MOTORISÉS ; en zone rurale une distance en voiture est normale et attendue.\n\n"
        f"LIEUX À JUGER ({len(batch)}) :\n{poi_block}\n\n"
        f"Réponds UNIQUEMENT par un objet JSON valide, sans markdown :\n"
        f'{{"verdicts": [{{"id": "...", "verdict": "keep" ou "reject", '
        f'"confidence": 0.0 à 1.0, "reason": "une phrase courte"}}]}}\n'
        f"Un verdict par id fourni, ni plus ni moins. Rappel : garde par défaut, ne "
        f"rejette QUE sur un motif de bruit explicite.")


# ── Parsing d'un verdict ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Verdict:
    verdict: str          # 'keep' | 'reject'
    confidence: float     # 0..1
    reason: str


# Robustesse (spec V2-45 1bis, §5) : tout POI DOIT recevoir un verdict exploitable. Un
# id que le juge n'a pas rendu (JSON tronqué, lot incomplet) reçoit ce défaut PRUDENT —
# `keep` confiance 0 : ne détruit jamais de valeur, et est SIGNALÉ dans le rapport.
DEFAULT_VERDICT = Verdict("keep", 0.0, "verdict par défaut (non rendu par le juge)")


def parse_verdicts(data: dict) -> dict[str, Verdict]:
    """Extrait `{id: Verdict}` d'un objet `{"verdicts":[...]}`. Tolère les champs
    manquants / mal typés : un verdict hors ('keep','reject') ou un id vide est ignoré
    (l'id manquant sera comblé par `finalize_verdicts` → jamais « non jugé »)."""
    out: dict[str, Verdict] = {}
    for v in (data or {}).get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or "").strip()
        verdict = str(v.get("verdict") or "").strip().lower()
        if not vid or verdict not in ("keep", "reject"):
            continue
        try:
            conf = float(v.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        out[vid] = Verdict(verdict, conf, str(v.get("reason") or "").strip())
    return out


def finalize_verdicts(pois: list[dict],
                      verdicts: dict[str, Verdict]) -> tuple[dict[str, Verdict], list[str]]:
    """Comble par `DEFAULT_VERDICT` tout POI sans verdict exploitable (§5). Renvoie
    `(verdicts_complets, ids_par_défaut)` — les seconds sont SIGNALÉS au rapport. PUR."""
    complete = dict(verdicts)
    defaulted: list[str] = []
    for p in pois:
        if p["id"] not in complete:
            complete[p["id"]] = DEFAULT_VERDICT
            defaulted.append(p["id"])
    return complete, defaulted


# ── Jugement (l'appel Claude est INJECTÉ → testable sans réseau) ──────────────

def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _judge_batches(prop: dict, pois: list[dict], zt: str, batch_size: int,
                   ask: Callable[[str], tuple[dict, dict]],
                   verdicts: dict[str, Verdict], attempts: list[dict],
                   label: str = "lot") -> None:
    """Juge `pois` par lots et met à jour `verdicts`/`attempts` en place."""
    batches = list(_chunks(pois, batch_size))
    for n, batch in enumerate(batches, 1):
        log.info("· %s %d/%d (%d lieux)…", label, n, len(batches), len(batch))
        data, meta = ask(build_prompt(prop, batch, zone_type=zt))
        verdicts.update(parse_verdicts(data))
        attempts.extend(meta.get("attempts")
                        or [{"units": meta.get("units", 0),
                             "cost_cts": meta.get("cost_cts", 0.0)}])


def judge_pois(prop: dict, pois: list[dict],
               ask: Callable[[str], tuple[dict, dict]], *,
               batch_size: int = 15) -> tuple[dict[str, Verdict], list[dict]]:
    """Juge les POI par lots. `ask(prompt) -> (data, meta)` est injecté (réel : appel
    Claude ; test : bouchon). Renvoie ({id: Verdict}, attempts) où `attempts` est la
    liste des coûts par essai à comptabiliser dans `api_costs`. Le signal rural/urbain
    est calculé UNE fois sur TOUTE la moisson (densité) et fourni à chaque lot.

    V2-45 1ter §4 : un POI resté SANS verdict exploitable (échec de parsing du lot) est
    RE-SOUMIS une fois — en petits lots — AVANT le verdict par défaut (3 ratés sur 243
    jugements cumulés). Le retry est BORNÉ à une passe (pas de boucle)."""
    zt = zone_hint(pois)
    verdicts: dict[str, Verdict] = {}
    attempts: list[dict] = []
    _judge_batches(prop, pois, zt, batch_size, ask, verdicts, attempts)
    missing = [p for p in pois if p["id"] not in verdicts]
    if missing:
        log.info("· retry : %d POI sans verdict re-soumis", len(missing))
        _judge_batches(prop, missing, zt, batch_size, ask, verdicts, attempts,
                       label="retry")
    return verdicts, attempts
