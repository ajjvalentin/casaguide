/* Moteur de recherche de l'aide (V2-31, volet 3a) — PUR (aucun DOM, aucun réseau).
 *
 * Technique de correspondance TOLÉRANTE, choisie et documentée :
 *   1. Normalisation : minuscules, accents retirés (NFD), ponctuation → espaces.
 *   2. Correspondance par TOKENS (mot à mot), en ignorant les mots-outils français,
 *      avec trois niveaux : exact (1.0), préfixe/sous-chaîne (0.7), et proximité par
 *      TRIGRAMMES (coefficient de Dice sur les 3-grammes de caractères ≥ 0.5) —
 *      c'est ce dernier qui rattrape les fautes de frappe (« wifii », « calendrer »).
 *   3. Bonus de sous-chaîne : si toute la requête normalisée apparaît telle quelle
 *      dans une entrée, le score est relevé (correspondance franche).
 *
 * Les trigrammes sont préférés à une distance d'édition globale (Levenshtein) car
 * ils restent robustes aux mots réordonnés et aux requêtes multi-mots, pour un coût
 * constant par entrée (ensembles pré-calculés une fois).
 *
 * Deux seuils :
 *   GOOD_SCORE      — au-dessus, on considère la réponse « franche » (sinon
 *                     « approchée » : la recherche ne rend JAMAIS un écran vide,
 *                     elle propose toujours les meilleures approches — veto André).
 *   COVERAGE_SCORE  — seuil de la COUVERTURE : un libellé du back-office est
 *                     « couvert » si, cherché tel quel, il trouve au moins une entrée
 *                     au-dessus de ce seuil. Le test de couverture s'appuie dessus.
 */

import { HELP_INDEX } from "./index.js";
import { buildIndex, normalize, search } from "../lib/matchcore.js";

// La normalisation est mutualisée dans le cœur (matchcore) ; on la re-exporte pour
// les consommateurs existants (le harnais de couverture).
export { normalize };

export const GOOD_SCORE = 0.45;
export const COVERAGE_SCORE = 0.5;

// Mots-outils français : ignorés dans les tokens (ils diluent la pertinence sans
// rien discriminer). Liste courte et volontairement stable. Le cœur est
// paramétré par CE jeu → comportement de l'aide strictement inchangé.
const STOPWORDS = new Set([
  "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "au", "aux", "en",
  "et", "ou", "a", "à", "dans", "par", "pour", "sur", "sous", "ce", "cet", "cette",
  "ces", "mon", "ma", "mes", "votre", "vos", "son", "sa", "ses", "que", "qui",
  "est", "il", "elle", "on", "se", "ne", "pas", "plus", "avec", "sans", "the",
]);

// Index pré-calculé (une fois) via le cœur partagé : le hay = question + mots-clés.
const PREPARED = buildIndex(HELP_INDEX, {
  textOf: (e) => [e.question, ...(e.keywords || [])],
  stopwords: STOPWORDS,
});

/**
 * Recherche dans l'index de l'aide. Renvoie TOUJOURS des résultats triés (jamais un
 * tableau vide si des approches existent) — l'écran zéro-résultat est interdit (§4).
 * @returns {{ results: Array<{entry, score}>, confident: boolean, top: number }}
 *   `confident` = le meilleur résultat dépasse GOOD_SCORE (sinon « approché »).
 */
export function searchHelp(query, { limit = 8 } = {}) {
  return search(PREPARED, query, { stopwords: STOPWORDS, limit, goodScore: GOOD_SCORE });
}

/** Un libellé est-il COUVERT par l'index ? (cœur du veto — cf. help-coverage). */
export function isCovered(label) {
  return searchHelp(label, { limit: 1 }).top >= COVERAGE_SCORE;
}

// ── Résolution des routes « M'y emmener » ────────────────────────────────────
//
// Les cibles de l'index sont des GABARITS de routes existantes (`:id` = logement
// courant). `resolveRoute` substitue l'id ; `routeIsValid` vérifie qu'un gabarit
// correspond à une forme de route servie par l'app (cf. app.js renderRoute) — le
// test s'en sert pour garantir que chaque « M'y emmener » mène quelque part.

// Formes de route servies par le back-office (gabarits, `:seg` = segment libre).
const ROUTE_SHAPES = [
  "#/properties",
  "#/abonnement",
  "#/properties/:id",
  "#/properties/:id/editor",
  "#/properties/:id/editor/staff",
  "#/properties/:id/pois",
  "#/properties/:id/pois/:filter",
  "#/properties/:id/calendrier",
];

function segs(route) {
  return route.replace(/^#\/?/, "").split("/").filter(Boolean);
}

/** Le gabarit de route correspond-il à une forme servie par l'app ? */
export function routeIsValid(route) {
  const rs = segs(route);
  return ROUTE_SHAPES.some((shape) => {
    const ss = segs(shape);
    if (ss.length !== rs.length) return false;
    return ss.every((s, i) => s.startsWith(":") || s === rs[i]);
  });
}

/**
 * Résout une route de cible pour la navigation : substitue `:id` par le logement
 * courant (déduit du hash actif) ; sans logement courant, une route ayant besoin
 * d'un `:id` retombe sur « Mes logements » (l'utilisateur choisit son logement).
 */
export function resolveRoute(route, currentHash = (typeof location !== "undefined" ? location.hash : "")) {
  if (!route.includes(":id")) return route;
  const m = String(currentHash).match(/#\/properties\/([^/?]+)/);
  if (m && m[1] !== "" ) return route.replace(":id", m[1]).replace("/:filter", "");
  return "#/properties";
}
