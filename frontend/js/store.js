/* État de session : jeton JWT en mémoire + sessionStorage, profil courant.
   Le jeton n'est jamais persisté en localStorage (durée de l'onglet uniquement). */

const TOKEN_KEY = "casaguide_token";

let _token = sessionStorage.getItem(TOKEN_KEY) || null;
let _owner = null;

export function getToken() { return _token; }

export function setToken(token) {
  _token = token || null;
  if (_token) sessionStorage.setItem(TOKEN_KEY, _token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

export function setOwner(owner) { _owner = owner; }
export function getOwner() { return _owner; }

export function clearSession() {
  _token = null;
  _owner = null;
  sessionStorage.removeItem(TOKEN_KEY);
}
