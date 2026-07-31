/* Analyse de chevauchement CÔTÉ NAVIGATEUR pour l'avertissement à la saisie
   (V2-23b, §0.4). Module **pur** (aucune dépendance DOM/réseau) : il reprend la
   règle d'intervalle **semi-ouvert** [arrivée, départ) du backend
   (`api/calendars.py`) pour prévenir instantanément, sur les séjours déjà chargés,
   ce que la période saisie recouvre. Aucun aller-retour serveur.

   ⚠ DUPLICATION VOLONTAIRE de la règle d'intervalle : le front AVERTIT, le back
   FAIT FOI (il recalcule chevauchements/rotations à chaque `GET /calendar`). Les
   deux doivent rester alignés — voir le piège correspondant dans CLAUDE.md.

   Les dates sont des chaînes ISO 'YYYY-MM-DD' : leur comparaison lexicographique
   est équivalente à la comparaison chronologique (aucune surprise de fuseau). */

const OCCUPIED_NATURES = new Set(["reservation", "private"]);

// « occupé » = quelqu'un dort ici → il y a une préparation. C'est cette question,
// et non « est-ce loué », qui pilote l'alerte de double réservation (§0.1).
export function isOccupied(b) {
  return b.status === "active"
    && OCCUPIED_NATURES.has(b.nature)
    && !b.linked_booking_id;
}

// Intervalle semi-ouvert : deux séjours qui se touchent (départ = arrivée) NE se
// chevauchent PAS (c'est une rotation).
export function intervalsOverlap(aStart, aEnd, bStart, bEnd) {
  return aStart < bEnd && bStart < aEnd;
}

function toMinutes(t) {
  if (!t) return null;
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

/* Analyse une période candidate contre les séjours chargés.
   `candidate` : { id?, starts_on, ends_on, checkin_time?, checkout_time? }
   `bookings`  : la liste `data.bookings` (nature, status, is_direct, eff_*…)
   `opts`      : { defaultCheckin, defaultCheckout } — heures standard du logement.

   Renvoie { red, neutral, rotations } :
   - red      : occupations recouvertes (double réservation) — demande un 2e clic
   - neutral  : recouvrements d'un works/unavailable/unqualified (informatif)
   - rotations: séjours qui se touchent (départ = arrivée) + fenêtre horaire. */
export function analyzeCandidate(candidate, bookings, opts = {}) {
  const red = [], neutral = [], rotations = [];
  const cs = candidate.starts_on, ce = candidate.ends_on;
  if (!cs || !ce || ce <= cs) return { red, neutral, rotations };

  const defIn = opts.defaultCheckin || "15:00";
  const defOut = opts.defaultCheckout || "10:00";
  const candIn = toMinutes(candidate.checkin_time) ?? toMinutes(defIn);
  const candOut = toMinutes(candidate.checkout_time) ?? toMinutes(defOut);

  for (const b of bookings || []) {
    if (candidate.id && b.id === candidate.id) continue;   // pas soi-même
    if (b.status === "cancelled") continue;                // annulé : ignoré
    if (b.linked_booking_id) continue;                     // bloc rattaché : masqué

    if (intervalsOverlap(cs, ce, b.starts_on, b.ends_on)) {
      (isOccupied(b) ? red : neutral).push(b);
    } else if (b.ends_on === cs) {
      // b part le jour où le candidat arrive → rotation, le candidat arrive après b.
      const dep = toMinutes(b.eff_checkout_time) ?? toMinutes(defOut);
      rotations.push({ booking: b, on: cs, role: "candidate_arrives",
                       gap_minutes: gap(dep, candIn) });
    } else if (ce === b.starts_on) {
      // le candidat part le jour où b arrive → rotation, b arrive après le candidat.
      const arr = toMinutes(b.eff_checkin_time) ?? toMinutes(defIn);
      rotations.push({ booking: b, on: ce, role: "candidate_departs",
                       gap_minutes: gap(candOut, arr) });
    }
  }
  return { red, neutral, rotations };
}

function gap(fromMin, toMin) {
  if (fromMin == null || toMin == null) return null;
  return toMin - fromMin;
}
