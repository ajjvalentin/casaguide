/* Règles d'entretien — module PUR (V2-23b, volet 1).

   Miroir front de `backend/api/care.py` pour ce que le navigateur doit calculer
   sans aller-retour serveur : les tranches d'âge par défaut et les SUGGESTIONS
   d'équipement à partir des âges des enfants (la modale propose, le propriétaire
   confirme — §1.0). Aucune dépendance DOM/réseau → testable directement en node.

   DUPLICATION VOLONTAIRE (comme overlaps.js) : le front SUGGÈRE, le back FAIT FOI
   (le moteur d'interventions calcule côté serveur/planning). Les deux règles de
   tranches d'âge doivent rester alignées — si l'une change, changer l'autre. */

// Tranches d'âge par défaut (repli si le logement n'a pas encore de care_rules).
// Alignées sur backend/api/care.default_care_rules().
export const DEFAULT_AGE_BANDS = [
  { code: "baby", max_age: 2, suggests: ["lit_bebe", "chaise_haute"], counts_as_adult: false },
  { code: "child", max_age: 12, suggests: ["lit_appoint"], counts_as_adult: false },
  { code: "teen", max_age: null, suggests: [], counts_as_adult: true },
];

/** Tranche d'âge d'un enfant (âge à l'arrivée) : première bande dont max_age est
 *  null (fourre-tout) ou age <= max_age. Bandes lues dans l'ordre croissant. */
export function ageBand(age, bands = DEFAULT_AGE_BANDS) {
  for (const b of bands) {
    if (b.max_age == null || age <= b.max_age) return b;
  }
  return null;
}

/** Codes d'équipement SUGGÉRÉS par les âges des enfants (§1.0). SUGGÈRE, n'ajoute
 *  jamais d'office. Sans doublon, dans l'ordre de première apparition. */
export function suggestEquipment(childrenAges, careRules) {
  const bands = (careRules && careRules.age_bands) || DEFAULT_AGE_BANDS;
  const out = [];
  for (const age of childrenAges || []) {
    const b = ageBand(Number(age), bands);
    if (!b) continue;
    for (const code of b.suggests || []) {
      if (!out.includes(code)) out.push(code);
    }
  }
  return out;
}

/** Nombre d'enfants = longueur de children_ages (jamais une colonne redondante). */
export function childrenCount(booking) {
  return ((booking && booking.children_ages) || []).length;
}
