/* Client de l'API CasaGuide.

   Toutes les données transitent par l'API existante (même origine que le
   back-office servi en statique : aucun problème CORS). Le jeton est joint
   automatiquement ; un 401 déclenche la déconnexion propre (handler injecté
   par app.js). Les erreurs réseau et applicatives sont converties en messages
   français exploitables par l'UI. */

import { getToken } from "./store.js";
import { messageFromDetail } from "./apierrors.js";

let _onUnauthorized = null;
export function setUnauthorizedHandler(fn) { _onUnauthorized = fn; }

// Suppression temporaire de l'intercepteur de session (V2-16). Sur les routes
// publiques (inscription / connexion) et au démarrage (sonde du jeton résiduel),
// un 401 ne doit JAMAIS déclencher l'éjection « session expirée » : il est géré
// localement (message sur le formulaire, nettoyage silencieux). Compteur ré-entrant.
let _suppressDepth = 0;
export function suppressUnauthorizedRedirect(on) {
  _suppressDepth += on ? 1 : -1;
  if (_suppressDepth < 0) _suppressDepth = 0;
}

export class ApiError extends Error {
  constructor(status, message, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function authHeaders(extra = {}, auth = true) {
  const headers = { ...extra };
  if (auth) {
    const t = getToken();
    if (t) headers["Authorization"] = "Bearer " + t;
  }
  return headers;
}

async function handleResponse(resp, auth) {
  if (resp.status === 401 && auth) {
    // Sur route publique / sonde de démarrage, on n'éjecte pas (V2-16).
    if (_onUnauthorized && _suppressDepth === 0) _onUnauthorized();
    throw new ApiError(401, "Votre session a expiré. Reconnectez-vous.");
  }
  if (resp.status === 204) return null;

  let data = null;
  if ((resp.headers.get("content-type") || "").includes("json")) {
    data = await resp.json().catch(() => null);
  }
  if (!resp.ok) {
    const detail = data && data.detail;
    // `detail` peut être une chaîne (erreurs classiques), un objet {code,message}
    // (refus de quota 402, V2-05a) OU une liste de validations Pydantic (422,
    // V2-16). On expose toujours un message FR lisible + on conserve `detail`
    // pour tester `detail.code` (quota).
    const msg = messageFromDetail(resp.status, detail);
    throw new ApiError(resp.status, msg, detail);
  }
  return data;
}

async function request(method, path, { body, auth = true } = {}) {
  const headers = authHeaders(body !== undefined ? { "Content-Type": "application/json" } : {}, auth);
  let resp;
  try {
    resp = await fetch(path, {
      method, headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, "Connexion au serveur impossible. Vérifiez votre réseau.");
  }
  return handleResponse(resp, auth);
}

/* Téléversement multipart : ne PAS fixer Content-Type (le navigateur ajoute la
   frontière). Le jeton est joint comme pour les requêtes JSON. */
async function upload(path, formData) {
  let resp;
  try {
    resp = await fetch(path, { method: "POST", headers: authHeaders(), body: formData });
  } catch (e) {
    throw new ApiError(0, "Connexion au serveur impossible. Vérifiez votre réseau.");
  }
  return handleResponse(resp, true);
}

/* Récupère un fichier protégé (média) avec le jeton et renvoie une URL objet
   utilisable comme src d'une image. À révoquer par l'appelant (URL.revokeObjectURL). */
async function fetchBlobUrl(path) {
  return URL.createObjectURL(await fetchBlob(path));
}

/* Récupère un fichier protégé (média, PDF…) avec le jeton et renvoie le Blob. */
async function fetchBlob(path) {
  let resp;
  try {
    resp = await fetch(path, { headers: authHeaders() });
  } catch (e) {
    throw new ApiError(0, "Connexion au serveur impossible. Vérifiez votre réseau.");
  }
  if (resp.status === 401) { if (_onUnauthorized) _onUnauthorized(); throw new ApiError(401, "Session expirée."); }
  if (!resp.ok) throw new ApiError(resp.status, "Fichier indisponible.");
  return resp.blob();
}

export const api = {
  // Registre des langues (V2-21a) — langues PUBLIÉES offertes par le produit.
  // Source unique (invariant 8 étendu) : jamais de liste de langues en dur.
  listLanguages:   () => request("GET", "/languages", { auth: false }),
  // Plans & abonnement (V2-05a)
  listPlans:       () => request("GET", "/api/plans", { auth: false }),
  getSubscription: () => request("GET", "/api/subscription"),
  // Paiement Stripe (V2-05b) : renvoient une URL de redirection (Checkout / portail)
  startCheckout:   (plan) => request("POST", "/api/billing/checkout", { body: { plan } }),
  openBillingPortal: () => request("POST", "/api/billing/portal"),
  // Add-on « logement supplémentaire » (V2-18b) : demande la quantité ; la valeur
  // effective revient par le webhook (le front affiche « mise à jour en cours »).
  updateAddons:    (quantity) => request("POST", "/api/billing/addons", { body: { quantity } }),
  // Changement d'offre in-place d'un abonné payant actif (V2-18d/e) : upgrade
  // immédiat (proration) ou downgrade programmé à l'échéance (jamais un Checkout).
  changePlan:      (plan) => request("POST", "/api/billing/change-plan", { body: { plan } }),
  // Annule un downgrade programmé encore non pris en compte (V2-18e).
  cancelScheduledChange: () => request("POST", "/api/billing/cancel-scheduled-change"),

  // Auth
  register: (b) => request("POST", "/api/auth/register", { body: b, auth: false }),
  login:    (b) => request("POST", "/api/auth/login", { body: b, auth: false }),
  me:       () => request("GET", "/api/auth/me"),
  // Mot de passe oublié / réinitialisation (V2-08) — sans session
  forgotPassword: (email) => request("POST", "/api/auth/forgot", { body: { email }, auth: false }),
  resetPassword:  (token, password) => request("POST", "/api/auth/reset", { body: { token, password }, auth: false }),
  // Vérification d'email (V2-08) : clic sur le lien (sans session) + renvoi (session)
  verifyEmail:        (token) => request("POST", "/api/auth/verify-email", { body: { token }, auth: false }),
  resendVerification: () => request("POST", "/api/auth/resend-verification"),

  // Logements
  listProperties: () => request("GET", "/api/properties"),
  createProperty: (b) => request("POST", "/api/properties", { body: b }),
  getProperty:    (id) => request("GET", `/api/properties/${id}`),
  updateProperty: (id, b) => request("PATCH", `/api/properties/${id}`, { body: b }),
  deleteProperty: (id) => request("DELETE", `/api/properties/${id}`),
  stats:          (id) => request("GET", `/api/properties/${id}/stats`),
  recomputeDistances: (id) => request("POST", `/api/properties/${id}/recompute-distances`),
  // (Re)géocodage explicite de l'adresse + recalcul des distances (M-24)
  geocodeProperty: (id) => request("POST", `/api/properties/${id}/geocode`),
  // Affiche QR imprimable (M-07) — PDF protégé récupéré comme Blob (jeton joint).
  // Langue du poster au choix (M-26) : fr|en|es.
  posterBlob: (id, { size, lang } = {}) => {
    const q = new URLSearchParams();
    if (size) q.set("size", size);
    if (lang) q.set("lang", lang);
    const qs = q.toString();
    return fetchBlob(`/api/properties/${id}/guide-poster.pdf` + (qs ? `?${qs}` : ""));
  },

  // Secrets chiffrés
  getSecrets: (id) => request("GET", `/api/properties/${id}/secrets`),
  putSecrets: (id, b) => request("PUT", `/api/properties/${id}/secrets`, { body: b }),

  // Sections du guide
  listSections: (id) => request("GET", `/api/properties/${id}/sections`),
  putSection:   (id, code, b) => request("PUT", `/api/properties/${id}/sections/${code}`, { body: b }),

  // POI
  listPois:   (id, status) =>
    request("GET", `/api/properties/${id}/pois` + (status ? `?status=${status}` : "")),
  approvePoi: (id, poi) => request("POST", `/api/properties/${id}/pois/${poi}/approve`),
  rejectPoi:  (id, poi) => request("POST", `/api/properties/${id}/pois/${poi}/reject`),
  setPoiStatus: (id, poi, status) => request("POST", `/api/properties/${id}/pois/${poi}/status`, { body: { status } }),
  editPoi:    (id, poi, b) => request("PATCH", `/api/properties/${id}/pois/${poi}`, { body: b }),
  // Ajout manuel de lieux (M-22)
  poiCategories: (id) => request("GET", `/api/properties/${id}/pois/categories`),
  searchPois: (id, q) => request("GET", `/api/properties/${id}/pois/search?q=${encodeURIComponent(q)}`),
  createPoi:  (id, b) => request("POST", `/api/properties/${id}/pois`, { body: b }),

  // Enrichissement
  enrich:   (id, trigger) => request("POST", `/api/properties/${id}/enrich`, { body: { trigger } }),
  listJobs: (id) => request("GET", `/api/properties/${id}/jobs`),
  getJob:   (id, job) => request("GET", `/api/properties/${id}/jobs/${job}`),

  // Traductions du guide voyageur (M-09)
  translationStatus: (id) => request("GET", `/api/properties/${id}/translation-status`),
  translate:         (id) => request("POST", `/api/properties/${id}/translate`),

  // Calendrier des séjours (V2-23a)
  calendarView:   (id) => request("GET", `/api/properties/${id}/calendar`),
  createBooking:  (id, b) => request("POST", `/api/properties/${id}/bookings`, { body: b }),
  updateBooking:  (id, bid, b) => request("PATCH", `/api/properties/${id}/bookings/${bid}`, { body: b }),
  deleteBooking:  (id, bid) => request("DELETE", `/api/properties/${id}/bookings/${bid}`),
  listCalendars:  (id) => request("GET", `/api/properties/${id}/calendars`),
  addCalendar:    (id, b) => request("POST", `/api/properties/${id}/calendars`, { body: b }),
  deleteCalendar: (id, cid) => request("DELETE", `/api/properties/${id}/calendars/${cid}`),
  syncCalendars:  (id) => request("POST", `/api/properties/${id}/calendar/sync`),

  // Fenêtre « Envoyer le guide » (V2-23c, volet 3) : liens générés à la demande
  // (idempotents) + gabarits d'envoi localisés (email/WhatsApp).
  showcaseLink: (id) => request("POST", `/api/properties/${id}/showcase-link`),
  stayLink:     (id, bid) => request("POST", `/api/properties/${id}/bookings/${bid}/stay-link`),
  sendTemplates: (lang) => request("GET", `/send-templates?lang=${encodeURIComponent(lang)}`, { auth: false }),

  // Règles d'entretien, catalogue & demandes (V2-23b, volet 1)
  listRequestTypes:  (id) => request("GET", `/api/properties/${id}/request-types`),
  createRequestType: (id, b) => request("POST", `/api/properties/${id}/request-types`, { body: b }),
  updateRequestType: (id, tid, b) => request("PATCH", `/api/properties/${id}/request-types/${tid}`, { body: b }),
  listBookingRequests:  (id, bid) => request("GET", `/api/properties/${id}/bookings/${bid}/requests`),
  createBookingRequest: (id, bid, b) => request("POST", `/api/properties/${id}/bookings/${bid}/requests`, { body: b }),
  updateBookingRequest: (id, rid, b) => request("PATCH", `/api/properties/${id}/requests/${rid}`, { body: b }),
  deleteBookingRequest: (id, rid) => request("DELETE", `/api/properties/${id}/requests/${rid}`),
  bookingInterventions: (id, bid) => request("GET", `/api/properties/${id}/bookings/${bid}/interventions`),

  // Médias par section (M-12)
  listMedia:  (id, code) =>
    request("GET", `/api/properties/${id}/media` + (code ? `?section_code=${encodeURIComponent(code)}` : "")),
  uploadMedia: (id, formData) => upload(`/api/properties/${id}/media`, formData),
  updateMediaCaption: (id, mid, caption) =>
    request("PATCH", `/api/properties/${id}/media/${mid}`, { body: { caption } }),
  deleteMedia: (id, mid) => request("DELETE", `/api/properties/${id}/media/${mid}`),
  reorderMedia: (id, ids) => request("POST", `/api/properties/${id}/media/reorder`, { body: { ids } }),
  mediaBlobUrl: (id, mid) => fetchBlobUrl(`/api/properties/${id}/media/${mid}/file`),
};
