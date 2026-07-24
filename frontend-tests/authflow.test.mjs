/* Tests de l'enchaînement d'inscription / connexion (V2-16) — `frontend/js/authflow.js`.

   Exécuter : node --test frontend-tests/

   Reproduit, hors navigateur, le parcours EXACT constaté en production le 24/07 :
   création → jeton → /me → Checkout → redirection, et chaque cas d'erreur. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { submitAuth } from "../frontend/js/authflow.js";

// Fabrique un jeu de `deps` mockées + un journal d'appels inspectable.
function makeDeps(over = {}) {
  const calls = [];
  const state = { token: null, owner: null, suppress: 0, maxSuppress: 0, nav: null, redirected: null, toasts: [] };
  const rec = (name) => (...args) => { calls.push([name, ...args]); };
  const deps = {
    register: async (b) => { calls.push(["register", b]); return { access_token: "tok-new" }; },
    login: async (b) => { calls.push(["login", b]); return { access_token: "tok-login" }; },
    me: async () => { calls.push(["me"]); return { id: "o1", email: "x@y.z" }; },
    startCheckout: async (p) => { calls.push(["startCheckout", p]); return { url: "https://checkout.test/s" }; },
    setToken: (t) => { calls.push(["setToken", t]); state.token = t; },
    setOwner: (o) => { calls.push(["setOwner", o]); state.owner = o; },
    redirect: (u) => { calls.push(["redirect", u]); state.redirected = u; },
    navigate: (h) => { calls.push(["navigate", h]); state.nav = h; },
    toast: (m, t) => { calls.push(["toast", m, t]); state.toasts.push(m); },
    suppress: (on) => {
      state.suppress += on ? 1 : -1;
      state.maxSuppress = Math.max(state.maxSuppress, state.suppress);
      calls.push(["suppress", on]);
    },
    ...over,
  };
  return { deps, calls, state };
}

const names = (calls) => calls.map((c) => c[0]);

test("inscription gratuite : jeton posé + /me vérifié + entrée back-office", async () => {
  const { deps, calls, state } = makeDeps();
  const r = await submitAuth({
    mode: "register", planId: "free",
    credentials: { email: "a@b.c", password: "password123", full_name: "A" }, deps,
  });
  assert.deepEqual(r, { outcome: "properties" });
  assert.equal(state.nav, "#/properties");
  assert.equal(state.redirected, null);            // pas de Checkout
  // Ordre : jeton posé AVANT /me, aucun startCheckout.
  const idxToken = names(calls).indexOf("setToken");
  const idxMe = names(calls).indexOf("me");
  assert.ok(idxToken >= 0 && idxToken < idxMe, "setToken avant me()");
  assert.ok(!names(calls).includes("startCheckout"));
  assert.equal(state.suppress, 0, "suppression relâchée en fin de parcours");
  assert.ok(state.maxSuppress >= 1, "intercepteur suspendu pendant le parcours");
});

test("inscription payante : Checkout après jeton+/me, redirection obtenue", async () => {
  const { deps, calls, state } = makeDeps();
  const r = await submitAuth({
    mode: "register", planId: "solo",
    credentials: { email: "a@b.c", password: "password123", full_name: "A" }, deps,
  });
  assert.deepEqual(r, { outcome: "redirect" });
  assert.equal(state.redirected, "https://checkout.test/s");
  // La session (jeton + /me) est établie AVANT l'appel billing.
  const order = names(calls);
  assert.ok(order.indexOf("setToken") < order.indexOf("startCheckout"));
  assert.ok(order.indexOf("me") < order.indexOf("startCheckout"));
  assert.ok(order.indexOf("startCheckout") < order.indexOf("redirect"));
});

test("Checkout en échec : connecté sur #/abonnement, jamais éjecté", async () => {
  const { deps, calls, state } = makeDeps({
    startCheckout: async () => { throw new Error("503 indispo"); },
  });
  const r = await submitAuth({
    mode: "register", planId: "solo",
    credentials: { email: "a@b.c", password: "password123", full_name: "A" }, deps,
  });
  assert.equal(r.outcome, "abonnement");
  assert.match(r.message, /Mon abonnement/);
  assert.equal(state.nav, "#/abonnement");         // connecté, pas #/properties ni #/login
  assert.equal(state.token, "tok-new");            // session conservée
  assert.ok(state.toasts.some((m) => /Compte créé/.test(m)));
});

test("email invalide (422) : reste sur le formulaire avec message lisible, jeton non posé", async () => {
  const err = new Error("Adresse email invalide.");
  err.status = 422;
  const { deps, calls, state } = makeDeps({
    register: async () => { throw err; },
  });
  const r = await submitAuth({
    mode: "register", planId: "solo",
    credentials: { email: "x@foo", password: "password123", full_name: "A" }, deps,
  });
  assert.deepEqual(r, { outcome: "error", message: "Adresse email invalide." });
  assert.equal(state.token, null);                 // jamais de session partielle
  assert.equal(state.nav, null);                   // aucune navigation
  assert.equal(state.suppress, 0);                 // suppression relâchée
});

test("401 sur /me après création : géré localement, aucune éjection globale", async () => {
  // La suppression est active pendant tout le parcours → un 401 sur /me ne
  // déclenche pas l'intercepteur (il lèverait normalement ApiError(401)).
  const err = new Error("Votre session a expiré. Reconnectez-vous.");
  err.status = 401;
  const { deps, state } = makeDeps({ me: async () => { throw err; } });
  const r = await submitAuth({
    mode: "register", planId: "free",
    credentials: { email: "a@b.c", password: "password123", full_name: "A" }, deps,
  });
  assert.equal(r.outcome, "error");
  assert.equal(state.maxSuppress >= 1, true);      // intercepteur bien suspendu
  assert.equal(state.suppress, 0);
  assert.equal(state.nav, null);                   // pas d'éjection vers login/accueil
});

test("connexion (login) : pas de plan, entrée directe", async () => {
  const { deps, state } = makeDeps();
  const r = await submitAuth({
    mode: "login", planId: "free",
    credentials: { email: "a@b.c", password: "password123" }, deps,
  });
  assert.deepEqual(r, { outcome: "properties" });
  assert.equal(state.nav, "#/properties");
});

test("connexion avec plan sélectionné ignoré : le Checkout n'est jamais déclenché hors inscription", async () => {
  const { deps, calls } = makeDeps();
  await submitAuth({
    mode: "login", planId: "solo",  // planId résiduel : ignoré car mode != register
    credentials: { email: "a@b.c", password: "password123" }, deps,
  });
  assert.ok(!names(calls).includes("startCheckout"));
});
