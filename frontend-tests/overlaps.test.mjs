/* Test du module pur d'analyse de chevauchement à la saisie (V2-23b, §0.4).

   `frontend/js/lib/overlaps.js` n'a AUCUNE dépendance DOM/réseau → il s'importe
   directement dans node (aucun Chrome requis). On vérifie qu'il reprend fidèlement
   la règle d'intervalle semi-ouvert du backend : rotation ≠ chevauchement, et que
   seule une OCCUPATION (reservation/private) est classée « rouge ».

   Exécuter : node --test frontend-tests/ */

import { test } from "node:test";
import assert from "node:assert/strict";
import { analyzeCandidate, isOccupied, intervalsOverlap }
  from "../frontend/js/lib/overlaps.js";

function bk(over) {
  return { id: "x", starts_on: "2026-08-10", ends_on: "2026-08-20",
           nature: "reservation", status: "active", is_direct: false,
           linked_booking_id: null, eff_checkin_time: "15:00:00",
           eff_checkout_time: "10:00:00", ...over };
}
const OPTS = { defaultCheckin: "15:00", defaultCheckout: "10:00" };

test("occupation recouverte → rouge (double réservation)", () => {
  const existing = [bk({ id: "a", starts_on: "2026-08-10", ends_on: "2026-08-20" })];
  const r = analyzeCandidate(
    { starts_on: "2026-08-15", ends_on: "2026-08-25" }, existing, OPTS);
  assert.equal(r.red.length, 1);
  assert.equal(r.neutral.length, 0);
  assert.equal(r.rotations.length, 0);
});

test("occupation PRIVÉE recouvrant une réservation compte comme rouge", () => {
  const existing = [bk({ id: "a", nature: "private" })];
  const r = analyzeCandidate(
    { starts_on: "2026-08-15", ends_on: "2026-08-25" }, existing, OPTS);
  assert.equal(r.red.length, 1);
});

test("recouvrement d'un works/unqualified → neutre, pas rouge", () => {
  const existing = [
    bk({ id: "a", nature: "works" }),
    bk({ id: "b", nature: "unqualified" }),
  ];
  const r = analyzeCandidate(
    { starts_on: "2026-08-15", ends_on: "2026-08-25" }, existing, OPTS);
  assert.equal(r.red.length, 0);
  assert.equal(r.neutral.length, 2);
});

test("séjours qui se touchent → rotation, pas chevauchement + fenêtre calculée", () => {
  // a part le 20 ; le candidat arrive le 20 → rotation, fenêtre 10:00 → 15:00 = 5 h.
  const existing = [bk({ id: "a", starts_on: "2026-08-10", ends_on: "2026-08-20" })];
  const r = analyzeCandidate(
    { starts_on: "2026-08-20", ends_on: "2026-08-27" }, existing, OPTS);
  assert.equal(r.red.length, 0);
  assert.equal(r.rotations.length, 1);
  assert.equal(r.rotations[0].on, "2026-08-20");
  assert.equal(r.rotations[0].gap_minutes, 300);
});

test("séjour annulé, rattaché ou soi-même : ignorés", () => {
  const existing = [
    bk({ id: "self", starts_on: "2026-08-12", ends_on: "2026-08-22" }),
    bk({ id: "cancel", status: "cancelled", starts_on: "2026-08-12", ends_on: "2026-08-22" }),
    bk({ id: "linked", linked_booking_id: "self", starts_on: "2026-08-12", ends_on: "2026-08-22" }),
  ];
  const r = analyzeCandidate(
    { id: "self", starts_on: "2026-08-12", ends_on: "2026-08-22" }, existing, OPTS);
  assert.equal(r.red.length, 0);
  assert.equal(r.neutral.length, 0);
});

test("garde-fous d'API pure", () => {
  assert.equal(isOccupied(bk({ nature: "reservation", status: "active" })), true);
  assert.equal(isOccupied(bk({ nature: "works" })), false);
  assert.equal(isOccupied(bk({ status: "cancelled" })), false);
  assert.equal(intervalsOverlap("2026-08-10", "2026-08-20", "2026-08-20", "2026-08-27"), false);
  assert.equal(intervalsOverlap("2026-08-10", "2026-08-21", "2026-08-20", "2026-08-27"), true);
});
