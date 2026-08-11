/* Cœur de correspondance tolérante — PUR, PARTAGÉ, PARAMÉTRABLE (V2-33 volet 2).
 *
 * Extrait de `help/search.js` (recherche d'aide back-office, V2-31 volet 3a) pour
 * être réutilisé par la recherche du GUIDE VOYAGEUR (hors-ligne, 7 langues). Aucun
 * DOM, aucun réseau, aucune dépendance : deux consommateurs, une seule technique.
 *
 * Technique (inchangée depuis le 3a) :
 *   1. Normalisation : minuscules, ß→ss, accents retirés (NFD), ponctuation →
 *      espaces, compactage.
 *   2. Correspondance par TOKENS (mot à mot), les mots-outils fournis par
 *      l'appelant étant ignorés (l'aide passe ses stopwords français ; le guide un
 *      jeu minimal par langue, ou vide). Trois niveaux : exact (1.0), préfixe/
 *      sous-chaîne (0.7), proximité par TRIGRAMMES (Dice ≥ 0.5) — ce dernier
 *      rattrape les fautes de frappe.
 *   3. Bonus de sous-chaîne : la requête entière retrouvée telle quelle → 0.95.
 *
 * PARAMÉTRABLE : l'appelant fournit `stopwords` (à `buildIndex` ET `search`), le
 * texte indexable de chaque entrée (`textOf`), et les seuils (`goodScore`,
 * `fallback`). Le comportement par défaut reproduit EXACTEMENT celui du 3a.
 */

// Jeu de mots-outils vide par défaut (le guide multilingue n'en impose aucun).
export const NO_STOPWORDS = new Set();

/** Normalise : minuscules, ß→ss (sinon perdu par le filtre), accents retirés,
 *  ponctuation → espaces, compactage. Couvre fr/es/it/nl/sq (NFD) + de (ß→ss). */
export function normalize(s) {
  return String(s == null ? "" : s)
    .toLowerCase()
    .replace(/ß/g, "ss")                       // eszett : NFD ne le décompose pas
    .normalize("NFD").replace(/[̀-ͯ]/g, "")   // diacritiques
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

/** Tokens signifiants (mots-outils fournis et unités < 2 caractères écartés). */
export function tokenize(norm, stopwords = NO_STOPWORDS) {
  return norm.split(" ").filter((w) => w.length >= 2 && !stopwords.has(w));
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

/**
 * Prépare un index recherchable : pour chaque entrée, son texte normalisé (le
 * « hay ») et ses tokens (avec trigrammes), calculés UNE fois.
 * @param entries  tableau d'entrées quelconques
 * @param textOf   (entry) → tableau de chaînes à indexer (titre, mots-clés, corps…)
 * @param stopwords Set de mots-outils à ignorer dans les tokens
 */
export function buildIndex(entries, { textOf, stopwords = NO_STOPWORDS } = {}) {
  return entries.map((entry) => {
    const parts = textOf(entry).map(normalize);
    const hay = parts.join(" ");
    const toks = [...new Set(parts.flatMap((p) => tokenize(p, stopwords)))];
    return { entry, hay, tokens: toks.map((t) => ({ t, tri: trigrams(t) })) };
  });
}

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

/** Score global d'une requête normalisée pour une entrée préparée (0..1). */
export function scoreEntry(qNorm, qTokens, prepared) {
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
 * Recherche dans un index préparé. Renvoie TOUJOURS des résultats triés (jamais un
 * tableau vide si des approches existent) — l'écran zéro-résultat est interdit
 * (veto André, hérité du 3a).
 * @returns {{ results: Array<{entry, score}>, confident: boolean, top: number }}
 */
export function search(index, query, {
  stopwords = NO_STOPWORDS, limit = 8, goodScore = 0.45, fallback = 5,
} = {}) {
  const qNorm = normalize(query);
  const qTokens = tokenize(qNorm, stopwords).map((t) => ({ t, tri: trigrams(t) }));
  const scored = index
    .map((p) => ({ entry: p.entry, score: scoreEntry(qNorm, qTokens, p) }))
    .sort((a, b) => b.score - a.score);
  const top = scored.length ? scored[0].score : 0;
  // Résultats « francs » au-dessus du seuil ; à défaut, les meilleures approches.
  const strong = scored.filter((r) => r.score >= goodScore).slice(0, limit);
  const results = strong.length ? strong : scored.slice(0, Math.min(limit, fallback));
  return { results, confident: top >= goodScore, top };
}
