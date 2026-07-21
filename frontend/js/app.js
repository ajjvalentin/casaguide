/* Point d'entrée du back-office : bootstrap de session, ossature (barre
   supérieure) et routage par ancre.

   Routes :
     #/login                         connexion / inscription
     #/properties                    mes logements
     #/properties/:id/editor         éditeur de guide (M-03) + position (M-05)
     #/properties/:id/pois[/:filter] validation des suggestions (M-04),
                                     filtre initial optionnel (V2-11) */

import { api, setUnauthorizedHandler } from "./api.js";
import { getToken, getOwner, setOwner, clearSession } from "./store.js";
import { el, icon, mount, clear, toast, refreshIcons } from "./ui.js";
import { navigate } from "./nav.js";
import { renderLogin } from "./views/login.js";
import { renderForgot, renderReset } from "./views/reset.js";
import { renderProperties } from "./views/properties.js";
import { renderEditor } from "./views/editor.js";
import { renderPois } from "./views/pois.js";

const appEl = document.getElementById("app");

function logout() {
  clearSession();
  toast("Vous êtes déconnecté.", "ok");
  navigate("#/login");
}

// Construit (une fois) la barre supérieure + le conteneur de vue, renvoie la vue.
function ensureShell() {
  let viewEl = document.getElementById("app-view");
  if (viewEl) { updateUserMenu(); return viewEl; }

  const userMenu = el("div", { class: "usermenu", id: "usermenu" });
  const topbar = el("header", { class: "topbar" },
    el("a", { class: "brand", href: "#/properties" },
      el("span", { class: "mark" }, icon("map-pinned", 18)), "CasaGuide"),
    el("span", { class: "spacer" }),
    userMenu);
  viewEl = el("main", { id: "app-view" });
  mount(appEl, topbar, viewEl);
  updateUserMenu();
  return viewEl;
}

function updateUserMenu() {
  const menu = document.getElementById("usermenu");
  if (!menu) return;
  const owner = getOwner();
  mount(menu,
    owner ? el("span", { class: "email" }, icon("user", 15), " ", owner.email) : null,
    el("button", { class: "btn btn-sm btn-ghost", onClick: logout }, icon("log-out", 15), "Déconnexion"));
}

function renderRoute() {
  // Routes publiques (accessibles sans session) : mot de passe oublié /
  // réinitialisation via lien à jeton reçu par email (V2-08).
  const hash = location.hash;
  if (hash === "#/forgot") { renderForgot(appEl); return; }
  if (hash.startsWith("#/reset/")) {
    return void renderReset(appEl, decodeURIComponent(hash.slice("#/reset/".length)));
  }

  if (!getToken()) { renderLogin(appEl); return; }

  const view = ensureShell();
  const seg = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);

  if (!seg.length || seg[0] !== "properties") { navigate("#/properties"); return; }
  if (seg.length === 1) return void renderProperties(view);

  const pid = seg[1];
  // #/properties/:id/pois[/:filter] — le 4e segment (V2-11) pré-sélectionne un
  // filtre de la vue POI (deep-link depuis les pastilles de « Mes logements »).
  if (seg[2] === "pois") return void renderPois(view, pid, seg[3]);
  return void renderEditor(view, pid); // éditeur par défaut
}

async function boot() {
  setUnauthorizedHandler(() => { clearSession(); toast("Votre session a expiré.", "err"); renderRoute(); });

  // Jeton présent mais profil non chargé (rechargement d'onglet) → valider
  if (getToken() && !getOwner()) {
    try { setOwner(await api.me()); } catch (_) { clearSession(); }
  }

  window.addEventListener("hashchange", renderRoute);
  refreshIcons();
  renderRoute();
}

boot();
