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

export const GOOD_SCORE = 0.45;
export const COVERAGE_SCORE = 0.5;

// Mots-outils français : ignorés dans les tokens (ils diluent la pertinence sans
// rien discriminer). Liste courte et volontairement stable.
const STOPWORDS = new Set([
  "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "au", "aux", "en",
  "et", "ou", "a", "à", "dans", "par", "pour", "sur", "sous", "ce", "cet", "cette",
  "ces", "mon", "ma", "mes", "votre", "vos", "son", "sa", "ses", "que", "qui",
  "est", "il", "elle", "on", "se", "ne", "pas", "plus", "avec", "sans", "the",
]);

/** Normalise : minuscules, accents retirés, ponctuation → espaces, compactage. */
export function normalize(s) {
  return String(s == null ? "" : s)
    .toLowerCase()
    .normalize("NFD").replace(/[̀-ͯ]/g, "")   // diacritiques
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

/** Tokens signifiants (mots-outils et unités < 2 caractères écartés). */
function tokens(norm) {
  return norm.split(" ").filter((w) => w.length >= 2 && !STOPWORDS.has(w));
}

/** Ensemble des trigrammes de caractères d'un mot (bordé d'espaces). */
function trigrams(word) {
  const w = "  " + word + "  ";
  const set = new Set();
  for (let i = 0; i < w.length - 2; i++) set.add(w.slice(i, i + 3));
  return set;
}

/** Coefficient de Dice entre deux ensembles de trigrammes (0..1). */
function dice(a, b) {
  if (!a.size || !b.size) return 0;
  let inter = 0;
  for (const g of a) if (b.has(g)) inter++;
  return (2 * inter) / (a.size + b.size);
}

// Index pré-calculé : pour chaque entrée, son texte normalisé (question + mots-clés)
// et ses tokens (avec trigrammes) — calculé une fois au chargement du module.
const PREPARED = HELP_INDEX.map((entry) => {
  const hayParts = [entry.question, ...(entry.keywords || [])].map(normalize);
  const hay = hayParts.join(" ");
  const tokenList = [...new Set(hayParts.flatMap(tokens))];
  return {
    entry,
    hay,
    tokens: tokenList.map((t) => ({ t, tri: trigrams(t) })),
  };
});

/** Score de proximité d'un token de requête à une entrée préparée (0..1). */
function tokenScore(qt, qtri, prepared) {
  let best = 0;
  for (const { t, tri } of prepared.tokens) {
    if (t === qt) return 1;
    if (t.length >= 4 && qt.length >= 4 && (t.includes(qt) || qt.includes(t))) {
      best = Math.max(best, 0.7);
      continue;
    }
    const d = dice(qtri, tri);
    if (d >= 0.5) best = Math.max(best, d);
  }
  return best;
}

/** Score global d'une requête pour une entrée préparée (0..1). */
function scoreEntry(qNorm, qTokens, prepared) {
  let tokenAvg = 0;
  if (qTokens.length) {
    let sum = 0;
    for (const { t, tri } of qTokens) sum += tokenScore(t, tri, prepared);
    tokenAvg = sum / qTokens.length;
  }
  // Correspondance franche : la requête entière apparaît dans l'entrée.
  const substr = qNorm.length >= 3 && prepared.hay.includes(qNorm) ? 0.95 : 0;
  return Math.max(tokenAvg, substr);
}

/**
 * Recherche dans l'index. Renvoie TOUJOURS des résultats triés (jamais un tableau
 * vide si des approches existent) — l'écran zéro-résultat est interdit (§4).
 * @returns {{ results: Array<{entry, score}>, confident: boolean, top: number }}
 *   `confident` = le meilleur résultat dépasse GOOD_SCORE (sinon « approché »).
 */
export function searchHelp(query, { limit = 8 } = {}) {
  const qNorm = normalize(query);
  const qTokens = tokens(qNorm).map((t) => ({ t, tri: trigrams(t) }));
  const scored = PREPARED
    .map((p) => ({ entry: p.entry, score: scoreEntry(qNorm, qTokens, p) }))
    .sort((a, b) => b.score - a.score);

  const top = scored.length ? scored[0].score : 0;
  // Résultats « francs » au-dessus du seuil ; à défaut, les meilleures approches
  // (jamais rien à l'écran). On borne à `limit`.
  const strong = scored.filter((r) => r.score >= GOOD_SCORE).slice(0, limit);
  const results = strong.length ? strong : scored.slice(0, Math.min(limit, 5));
  return { results, confident: top >= GOOD_SCORE, top };
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
