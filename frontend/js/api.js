/* Client de l'API CasaGuide.

   Toutes les données transitent par l'API existante (même origine que le
   back-office servi en statique : aucun problème CORS). Le jeton est joint
   automatiquement ; un 401 déclenche la déconnexion propre (handler injecté
   par app.js). Les erreurs réseau et applicatives sont converties en messages
   français exploitables par l'UI. */

import { getToken } from "./store.js";

let _onUnauthorized = null;
export function setUnauthorizedHandler(fn) { _onUnauthorized = fn; }

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
    if (_onUnauthorized) _onUnauthorized();
    throw new ApiError(401, "Votre session a expiré. Reconnectez-vous.");
  }
  if (resp.status === 204) return null;

  let data = null;
  if ((resp.headers.get("content-type") || "").includes("json")) {
    data = await resp.json().catch(() => null);
  }
  if (!resp.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === "string" ? detail : `Erreur serveur (${resp.status}).`;
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
  // Auth
  register: (b) => request("POST", "/api/auth/register", { body: b, auth: false }),
  login:    (b) => request("POST", "/api/auth/login", { body: b, auth: false }),
  me:       () => request("GET", "/api/auth/me"),

  // Logements
  listProperties: () => request("GET", "/api/properties"),
  createProperty: (b) => request("POST", "/api/properties", { body: b }),
  getProperty:    (id) => request("GET", `/api/properties/${id}`),
  updateProperty: (id, b) => request("PATCH", `/api/properties/${id}`, { body: b }),
  deleteProperty: (id) => request("DELETE", `/api/properties/${id}`),
  stats:          (id) => request("GET", `/api/properties/${id}/stats`),
  recomputeDistances: (id) => request("POST", `/api/properties/${id}/recompute-distances`),
  // Affiche QR imprimable (M-07) — PDF protégé récupéré comme Blob (jeton joint)
  posterBlob: (id, size) => fetchBlob(`/api/properties/${id}/guide-poster.pdf` + (size ? `?size=${size}` : "")),

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
