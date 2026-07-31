/* Test du module pur des règles d'entretien côté front (V2-23b, §1.0).

   `frontend/js/lib/care.js` n'a AUCUNE dépendance DOM/réseau → il s'importe
   directement dans node. On vérifie l'alignement avec backend/api/care.py :
   suggestion d'équipement d'après les âges (SUGGÈRE, sans doublon).

   Exécuter : node --test frontend-tests/ */

import { test } from "node:test";
import assert from "node:assert/strict";
import { suggestEquipment, ageBand, childrenCount, DEFAULT_AGE_BANDS }
  from "../frontend/js/lib/care.js";

test("un bébé suggère lit bébé + chaise haute", () => {
  assert.deepEqual(suggestEquipment([1], null), ["lit_bebe", "chaise_haute"]);
});

test("un enfant suggère un lit d'appoint", () => {
  assert.deepEqual(suggestEquipment([8], null), ["lit_appoint"]);
});

test("un ado ne suggère rien (compte comme un adulte)", () => {
  assert.deepEqual(suggestEquipment([15], null), []);
});

test("suggestions dédoublonnées et dans l'ordre", () => {
  // deux bébés + un enfant : pas de doublon, ordre de première apparition.
  assert.deepEqual(suggestEquipment([1, 2, 8], null),
    ["lit_bebe", "chaise_haute", "lit_appoint"]);
});

test("aucun enfant → aucune suggestion", () => {
  assert.deepEqual(suggestEquipment([], null), []);
  assert.deepEqual(suggestEquipment(null, null), []);
});

test("tranches d'âge personnalisées prises en compte", () => {
  const rules = { age_bands: [
    { code: "petit", max_age: 5, suggests: ["truc"] },
    { code: "grand", max_age: null, suggests: [] },
  ] };
  assert.deepEqual(suggestEquipment([3], rules), ["truc"]);
  assert.deepEqual(suggestEquipment([10], rules), []);
});

test("ageBand : première bande dont max_age couvre l'âge", () => {
  assert.equal(ageBand(0, DEFAULT_AGE_BANDS).code, "baby");
  assert.equal(ageBand(12, DEFAULT_AGE_BANDS).code, "child");
  assert.equal(ageBand(13, DEFAULT_AGE_BANDS).code, "teen");
});

test("childrenCount = longueur de children_ages", () => {
  assert.equal(childrenCount({ children_ages: [1, 3, 14] }), 3);
  assert.equal(childrenCount({}), 0);
});
