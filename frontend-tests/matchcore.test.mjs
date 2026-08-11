/* Cœur de correspondance PARTAGÉ (V2-33 volet 2) — tests PURS (aucun navigateur).
 *
 * Vérifie le module extrait `frontend/js/lib/matchcore.js` : normalisation (dont
 * ß→ss), tokenisation PARAMÉTRÉE par les stopwords de l'appelant, et recherche
 * tolérante (exact, sous-chaîne, trigrammes) avec le repli « jamais vide ».
 *
 * Exécuter : node --test frontend-tests/
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { normalize, tokenize, buildIndex, search, NO_STOPWORDS }
  from "../frontend/js/lib/matchcore.js";

test("normalize : minuscules, ponctuation, accents — et ß→ss (allemand)", () => {
  assert.equal(normalize("Café  de   la Plage!"), "cafe de la plage");
  assert.equal(normalize("Straße"), "strasse");        // ß, sinon perdu par le filtre
  assert.equal(normalize("Fußweg"), "fussweg");
  assert.equal(normalize("Mühle"), "muhle");           // NFD (de/nl…)
  assert.equal(normalize("e hënë"), "e hene");         // albanais
  assert.equal(normalize(null), "");
});

test("tokenize : paramétré par les stopwords de l'appelant", () => {
  // Sans stopwords (défaut du guide multilingue) : tout mot ≥ 2 caractères.
  assert.deepEqual(tokenize(normalize("le marché du port")), ["le", "marche", "du", "port"]);
  // Avec stopwords français (comme l'aide back-office) : « le »/« du » filtrés.
  const fr = new Set(["le", "du", "la"]);
  assert.deepEqual(tokenize(normalize("le marché du port"), fr), ["marche", "port"]);
  // Les unités < 2 caractères sont toujours écartées.
  assert.deepEqual(tokenize(normalize("a b marché"), NO_STOPWORDS), ["marche"]);
});

const ENTRIES = [
  { id: "resto", text: ["Restaurant Zur Alten Mühle", "Regionale Küche"] },
  { id: "market", text: ["Straßenmarkt am Hafen", "Samstag"] },
  { id: "wifi", text: ["WLAN & Verbindung"] },
  { id: "beach", text: ["Playa del Cura", "plage"] },
];
const INDEX = buildIndex(ENTRIES, { textOf: (e) => e.text });
const topId = (q, opts) => search(INDEX, q, opts).results[0]?.entry.id;

test("search : correspondance exacte et par sous-chaîne", () => {
  assert.equal(topId("wlan"), "wifi");
  assert.equal(topId("hafen"), "market");
  assert.equal(topId("playa del cura"), "beach");     // sous-chaîne entière → franc
});

test("search : tolérance aux fautes (trigrammes)", () => {
  assert.equal(topId("muhle"), "resto");              // « Mühle »
  assert.equal(topId("restaurnt"), "resto");          // lettre manquante
  assert.equal(topId("verbindug"), "wifi");           // lettre manquante
});

test("search : ß→ss traverse toute la chaîne", () => {
  const r = search(INDEX, "strassenmarkt");
  assert.equal(r.results[0].entry.id, "market");
  assert.ok(r.confident, "correspondance franche attendue");
});

test("search : JAMAIS vide — une requête absurde propose des approches", () => {
  const r = search(INDEX, "xqzptvw");
  assert.ok(r.results.length > 0, "des approches sont proposées");
  assert.equal(r.confident, false, "mais rien de franc → confident=false");
});

test("search : seuil de confiance paramétrable (goodScore)", () => {
  // Un score de sous-chaîne (0.95) reste franc même avec un seuil élevé.
  assert.equal(search(INDEX, "wlan", { goodScore: 0.9 }).confident, true);
  // Un seuil > 1 rend tout « approché » (aucun score ne l'atteint).
  assert.equal(search(INDEX, "wlan", { goodScore: 1.1 }).confident, false);
});

test("search : limit borne les résultats francs, fallback borne les approches", () => {
  assert.ok(search(INDEX, "xqzptvw", { fallback: 2 }).results.length <= 2);
});
