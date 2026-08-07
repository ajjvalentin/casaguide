/* Moteur de recherche d'aide (V2-31, volet 3a) — tests PURS (aucun navigateur).

   Importe directement frontend/js/help/{index,search}.js (modules purs) : recherche
   exacte, tolérance aux fautes/accents, synonymes du terrain, zéro-résultat →
   approches (jamais un tableau vide), validité & résolution des routes « M'y
   emmener ». Ne nécessite pas Chrome → toujours exécuté (jamais « skip »).
   Exécuter : node --test frontend-tests/ */

import { test } from "node:test";
import assert from "node:assert/strict";

import { HELP_INDEX } from "../frontend/js/help/index.js";
import { searchHelp, isCovered, normalize, routeIsValid, resolveRoute, GOOD_SCORE }
  from "../frontend/js/help/search.js";

const topId = (q) => searchHelp(q).results[0]?.entry.id;

test("recherche exacte : le bon sujet remonte en tête", () => {
  assert.equal(topId("envoyer le guide"), "envoyer-guide");
  assert.equal(topId("publier le guide"), "publier-guide");
  assert.equal(topId("mon abonnement"), "abonnement");
  assert.equal(topId("rechercher les lieux"), "valider-suggestions");
});

test("tolérance aux fautes de frappe (trigrammes)", () => {
  const r = searchHelp("wifii");
  assert.equal(r.results[0].entry.id, "wifi");
  assert.ok(r.confident, "une faute proche doit rester une correspondance franche");
  assert.equal(topId("calendrer"), "importer-calendrier");
});

test("insensibilité aux accents et à la casse", () => {
  assert.equal(topId("BOITE A CLES"), "code-boite-cles");
  assert.equal(topId("equipe d entretien"), "equipe-entretien");
});

test("synonymes / vocabulaire du terrain", () => {
  // « airbnb » n'est pas un mot officiel de l'UI mais un mot du métier.
  const ids = searchHelp("airbnb").results.map((r) => r.entry.id);
  assert.ok(ids.includes("importer-calendrier") || ids.includes("dates-ajustees"),
    "« airbnb » doit trouver l'import de calendrier / les dates");
  assert.equal(topId("clé wifi"), "wifi");
  assert.equal(topId("code boîte"), "code-boite-cles");
});

test("zéro-résultat interdit : des approches, jamais un tableau vide", () => {
  const r = searchHelp("zorglub wxyz qwerty");
  assert.equal(r.confident, false, "un charabia ne doit pas être « franc »");
  assert.ok(r.top < GOOD_SCORE, "score sous le seuil de confiance");
  assert.ok(r.results.length > 0, "on rend TOUJOURS des rubriques proches (§4, veto)");
});

test("chaque cible « M'y emmener » résout vers une route servie", () => {
  for (const e of HELP_INDEX) {
    assert.ok(routeIsValid(e.target.route),
      `route invalide pour ${e.id} : ${e.target.route}`);
  }
});

test("résolution de :id contre le logement courant, repli sinon", () => {
  assert.equal(resolveRoute("#/properties/:id/calendrier", "#/properties/abc/editor"),
    "#/properties/abc/calendrier");
  assert.equal(resolveRoute("#/properties/:id/editor", "#/abonnement"), "#/properties");
  assert.equal(resolveRoute("#/abonnement", "#/properties/abc/editor"), "#/abonnement");
});

test("index bien formé : id, question, 2–4 étapes, cible", () => {
  const ids = new Set();
  for (const e of HELP_INDEX) {
    assert.ok(e.id && !ids.has(e.id), `id manquant ou dupliqué : ${e.id}`);
    ids.add(e.id);
    assert.ok(e.question && e.question.length > 4, `question faible : ${e.id}`);
    assert.ok(Array.isArray(e.steps) && e.steps.length >= 2 && e.steps.length <= 4,
      `${e.id} : 2 à 4 étapes attendues`);
    assert.ok(e.target && e.target.route && e.target.label, `cible incomplète : ${e.id}`);
    assert.ok(Array.isArray(e.keywords) && e.keywords.length > 0, `${e.id} : mots-clés manquants`);
  }
});

test("isCovered : un libellé du produit est couvert, un charabia ne l'est pas", () => {
  for (const l of ["Envoyer le guide", "Lieux suggérés", "Publier le guide",
    "Traductions", "Calendrier des séjours", "Mon abonnement"]) {
    assert.ok(isCovered(l), `devrait être couvert : ${l}`);
  }
  assert.equal(isCovered("Zorglub Wxyz Qwerty"), false);
});

test("normalize : minuscules, accents, ponctuation", () => {
  assert.equal(normalize("Boîte à clés…"), "boite a cles");
  assert.equal(normalize("QR à imprimer !"), "qr a imprimer");
});
