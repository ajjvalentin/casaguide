/* Tests du lien de partage multilingue (V2-10) — `frontend/js/share.js`.

   Hors `frontend/` (servi publiquement en statique). Exécuter avec :
       node --test frontend-tests/

   Couvre : lien slug conservé, ?lang=xx ajouté quand la langue diffère de la
   langue par défaut du logement, lien nu pour la langue par défaut / absente. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { guideSharePath, guideShareUrl } from "../frontend/js/share.js";

const PROP = { name: "Casa del Mar", guide_token: "abc123def456", default_lang: "fr" };
const SLUG = "/g/casa-del-mar-abc123def456";

test("sans lang : lien slug nu (rétrocompat)", () => {
  assert.equal(guideSharePath(PROP), SLUG);
});

test("langue par défaut : lien nu (pas de ?lang)", () => {
  assert.equal(guideSharePath(PROP, "fr"), SLUG);
});

test("langue différente : ?lang ajouté, slug conservé", () => {
  assert.equal(guideSharePath(PROP, "es"), `${SLUG}?lang=es`);
  assert.equal(guideSharePath(PROP, "en"), `${SLUG}?lang=en`);
});

test("langue insensible à la casse", () => {
  assert.equal(guideSharePath(PROP, "ES"), `${SLUG}?lang=es`);
  assert.equal(guideSharePath({ ...PROP, default_lang: "ES" }, "es"), SLUG);
});

test("default_lang absent → 'fr' par défaut", () => {
  const p = { name: "Casa del Mar", guide_token: "abc123def456" };
  assert.equal(guideSharePath(p), SLUG);
  assert.equal(guideSharePath(p, "fr"), SLUG);
  assert.equal(guideSharePath(p, "es"), `${SLUG}?lang=es`);
});

test("logement dont la langue par défaut est l'espagnol", () => {
  const p = { ...PROP, default_lang: "es" };
  assert.equal(guideSharePath(p, "es"), SLUG);            // défaut → nu
  assert.equal(guideSharePath(p, "fr"), `${SLUG}?lang=fr`); // autre → ?lang
});

test("guideShareUrl préfixe l'origine et conserve le slug", () => {
  globalThis.location = { origin: "https://guide.holaquetalimmo.es" };
  assert.equal(guideShareUrl(PROP), `https://guide.holaquetalimmo.es${SLUG}`);
  assert.equal(guideShareUrl(PROP, "es"), `https://guide.holaquetalimmo.es${SLUG}?lang=es`);
  delete globalThis.location;
});
