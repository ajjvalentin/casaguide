/* Point d'entrée du back-office : bootstrap de session, ossature (barre
   supérieure) et routage par ancre.

   Routes :
     #/login                         connexion / inscription
     #/properties                    mes logements
     #/properties/:id/editor         éditeur de guide (M-03) + position (M-05)
     #/properties/:id/pois[/:filter] validation des suggestions (M-04),
                                     filtre initial optionnel (V2-11) */

import { api, setUnauthorizedHandler, suppressUnauthorizedRedirect } from "./api.js";
import { getToken, getOwner, setOwner, clearSession } from "./store.js";
import { el, icon, mount, clear, toast, refreshIcons } from "./ui.js";
import { navigate } from "./nav.js";
import { renderLogin } from "./views/login.js";
import { renderForgot, renderReset, renderVerify } from "./views/reset.js";
import { renderProperties } from "./views/properties.js";
import { renderEditor } from "./views/editor.js";
import { renderPois } from "./views/pois.js";
import { renderSubscription } from "./views/subscription.js";

const appEl = document.getElementById("app");

function logout() {
  clearSession();
  toast("Vous êtes déconnecté.", "ok");
  navigate("#/login");
}

// Construit (une fois) la barre supérieure + le conteneur de vue, renvoie la vue.
function ensureShell() {
  let viewEl = document.getElementById("app-view");
  if (viewEl) { updateUserMenu(); updateVerifyBanner(); updateReadonlyBanner(); return viewEl; }

  const userMenu = el("div", { class: "usermenu", id: "usermenu" });
  const topbar = el("header", { class: "topbar" },
    el("a", { class: "brand", href: "#/properties" },
      el("span", { class: "mark" }, icon("map-pinned", 18)), "Holaguia"),
    el("span", { class: "spacer" }),
    userMenu);
  const banner = el("div", { id: "verify-banner" });
  const roBanner = el("div", { id: "readonly-banner" });
  viewEl = el("main", { id: "app-view" });
  mount(appEl, topbar, roBanner, banner, viewEl);
  updateUserMenu();
  updateVerifyBanner();
  updateReadonlyBanner();
  return viewEl;
}

// Bandeau global de lecture seule (V2-18a) : affiché quand l'essai est expiré
// (owner.trial_expired === true, strict — jamais sur un champ absent). Les guides
// restent en ligne ; seule l'édition est suspendue tant qu'une offre n'est pas
// choisie. CTA vers #/abonnement (les refus 403 sont aussi interceptés par quota.js).
function updateReadonlyBanner() {
  const box = document.getElementById("readonly-banner");
  if (!box) return;
  const owner = getOwner();
  if (!owner || owner.trial_expired !== true) { clear(box); return; }
  mount(box, el("div", { class: "readonly-banner" },
    icon("lock", 16),
    el("span", { class: "rb-msg" },
      "Votre essai est terminé — vos guides restent en ligne. Choisissez une "
      + "offre pour reprendre la main sur vos logements."),
    el("a", { class: "btn btn-sm btn-primary", href: "#/abonnement" },
      icon("credit-card", 15), "Choisir mon offre")));
}

// Bandeau discret « vérifiez votre email » (V2-08). Affiché uniquement quand le
// profil frais indique explicitement email_verified === false (jamais sur un
// profil au champ absent — évite les faux positifs sur un cache d'avant V2-08).
async function resendVerification(btn) {
  btn.disabled = true;
  try {
    const r = await api.resendVerification();
    toast((r && r.message) || "Email de vérification renvoyé.", "ok");
  } catch (_) {
    toast("Envoi impossible pour le moment.", "err");
    btn.disabled = false;
  }
}

function updateVerifyBanner() {
  const box = document.getElementById("verify-banner");
  if (!box) return;
  const owner = getOwner();
  if (!owner || owner.email_verified !== false) { clear(box); return; }
  const resend = el("button", { class: "btn btn-sm btn-ghost" },
    icon("mail", 15), "Renvoyer l'email");
  resend.addEventListener("click", () => resendVerification(resend));
  mount(box, el("div", { class: "verify-banner" },
    icon("mail-warning", 16),
    el("span", { class: "vb-msg" },
      "Confirmez votre adresse email pour sécuriser votre compte."),
    resend));
}

function updateUserMenu() {
  const menu = document.getElementById("usermenu");
  if (!menu) return;
  const owner = getOwner();
  mount(menu,
    owner ? el("span", { class: "email" }, icon("user", 15), " ", owner.email) : null,
    el("a", { class: "btn btn-sm btn-ghost", href: "#/abonnement" },
      icon("credit-card", 15), "Mon abonnement"),
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
  if (hash.startsWith("#/verify/")) {
    return void renderVerify(appEl, decodeURIComponent(hash.slice("#/verify/".length)));
  }

  if (!getToken()) { renderLogin(appEl); return; }

  const view = ensureShell();
  // On retire une éventuelle query (`?checkout=success`, V2-05b) avant de router :
  // seul le chemin d'ancre décide de la vue ; la query est lue par la vue.
  const seg = location.hash.replace(/^#\/?/, "").split("?")[0].split("/").filter(Boolean);

  if (seg[0] === "abonnement") return void renderSubscription(view);
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

  // Jeton présent mais profil non chargé (rechargement d'onglet) → valider.
  // Un jeton RÉSIDUEL/périmé ne doit pas déclencher l'éjection « session
  // expirée » (V2-16) : on encadre la sonde et on nettoie silencieusement.
  if (getToken() && !getOwner()) {
    suppressUnauthorizedRedirect(true);
    try { setOwner(await api.me()); }
    catch (_) { clearSession(); }
    finally { suppressUnauthorizedRedirect(false); }
  }

  window.addEventListener("hashchange", renderRoute);
  refreshIcons();
  renderRoute();
}

boot();
