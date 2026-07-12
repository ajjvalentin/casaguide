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

async function request(method, path, { body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const t = getToken();
    if (t) headers["Authorization"] = "Bearer " + t;
  }

  let resp;
  try {
    resp = await fetch(path, {
      method, headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, "Connexion au serveur impossible. Vérifiez votre réseau.");
  }

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

  // Enrichissement
  enrich:   (id, trigger) => request("POST", `/api/properties/${id}/enrich`, { body: { trigger } }),
  listJobs: (id) => request("GET", `/api/properties/${id}/jobs`),
  getJob:   (id, job) => request("GET", `/api/properties/${id}/jobs/${job}`),
};
