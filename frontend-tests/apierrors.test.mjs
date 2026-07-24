/* Tests du mapping d'erreurs d'API → message FR (V2-16) — `frontend/js/apierrors.js`.

   Exécuter : node --test frontend-tests/

   Couvre notamment le constat (c) : une erreur de validation 422 (LISTE Pydantic)
   ne doit plus afficher « Erreur serveur (422) » mais le détail lisible. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { messageFromDetail } from "../frontend/js/apierrors.js";

test("detail chaîne : renvoyé tel quel (erreurs applicatives)", () => {
  assert.equal(messageFromDetail(409, "Un compte existe déjà pour cet email"),
    "Un compte existe déjà pour cet email");
});

test("detail {code,message} : quota 402 (V2-05a) → message", () => {
  assert.equal(
    messageFromDetail(402, { code: "quota_exceeded", message: "Changez d'offre." }),
    "Changez d'offre.");
});

test("422 email invalide (liste Pydantic) → message FR lisible", () => {
  const detail = [{
    type: "value_error", loc: ["body", "email"],
    msg: "value is not a valid email address: ...", input: "x@foo",
  }];
  assert.equal(messageFromDetail(422, detail), "Adresse email invalide.");
});

test("422 mot de passe trop court → message FR", () => {
  const detail = [{
    type: "string_too_short", loc: ["body", "password"],
    msg: "String should have at least 8 characters",
  }];
  assert.equal(messageFromDetail(422, detail),
    "Mot de passe invalide (8 caractères minimum).");
});

test("422 multi-champs : messages dédoublonnés et joints", () => {
  const detail = [
    { loc: ["body", "email"], msg: "invalid" },
    { loc: ["body", "full_name"], msg: "too short" },
    { loc: ["body", "email"], msg: "invalid again" }, // doublon → ignoré
  ];
  assert.equal(messageFromDetail(422, detail),
    "Adresse email invalide. Indiquez votre nom complet.");
});

test("422 champ inconnu → libellé générique (jamais « Erreur serveur »)", () => {
  const detail = [{ loc: ["body", "wat"], msg: "nope" }];
  assert.equal(messageFromDetail(422, detail), "Un champ du formulaire est invalide.");
});

test("detail absent → repli « Erreur serveur (status) »", () => {
  assert.equal(messageFromDetail(500, null), "Erreur serveur (500).");
  assert.equal(messageFromDetail(503, undefined), "Erreur serveur (503).");
  assert.equal(messageFromDetail(500, []), "Erreur serveur (500).");
});
