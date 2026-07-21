/* Mot de passe oublié + réinitialisation (V2-08).

   Deux vues plein-écran (même identité que la connexion, sans session) :
     - renderForgot : demande d'un lien de réinitialisation. Message TOUJOURS
       neutre (« si ce compte existe, un email part ») — jamais d'indice sur
       l'existence du compte.
     - renderReset  : nouveau mot de passe à partir du jeton de l'URL
       (#/reset/{token}). Succès → invite à se connecter. */

import { api, ApiError } from "../api.js";
import { getToken, setOwner } from "../store.js";
import { el, icon, mount } from "../ui.js";
import { navigate } from "../nav.js";

function authCard(...children) {
  return el("div", { class: "auth-wrap" },
    el("div", { class: "card auth-card" },
      el("a", { class: "brand", href: "#/login" },
        el("span", { class: "mark" }, icon("map-pinned", 20)), "CasaGuide"),
      ...children));
}

// ── Demande d'un lien (« Mot de passe oublié ? ») ────────────────────────────

export function renderForgot(root) {
  const errBox = el("div", { class: "errbox hidden" });
  const okBox = el("div", { class: "okbox hidden" });
  const emailInput = el("input", { type: "email", name: "email",
    required: true, autocomplete: "email" });
  const submitBtn = el("button", { class: "btn btn-primary btn-block", type: "submit" },
    "Envoyer le lien");

  function showError(m) { errBox.textContent = m; errBox.classList.remove("hidden"); }

  async function onSubmit(e) {
    e.preventDefault();
    errBox.classList.add("hidden");
    const email = emailInput.value.trim();
    if (!email) return showError("Renseignez votre adresse email.");
    submitBtn.disabled = true;
    submitBtn.textContent = "Un instant…";
    try {
      const r = await api.forgotPassword(email);
      // Réponse volontairement neutre (anti-énumération) — on l'affiche telle quelle.
      okBox.textContent = (r && r.message) ||
        "Si un compte est associé à cette adresse, un email vient de partir.";
      okBox.classList.remove("hidden");
      form.classList.add("hidden");
    } catch (err) {
      showError(err instanceof ApiError ? err.message : "Une erreur est survenue.");
      submitBtn.disabled = false;
      submitBtn.textContent = "Envoyer le lien";
    }
  }

  const form = el("form", { onSubmit },
    el("div", { class: "field" },
      el("label", {}, "Adresse email"),
      el("div", { class: "help" }, "Nous vous enverrons un lien pour choisir un nouveau mot de passe."),
      emailInput),
    errBox,
    el("div", { style: { marginTop: "8px" } }, submitBtn));

  mount(root, authCard(
    el("h1", { class: "auth-title" }, "Mot de passe oublié"),
    okBox, form,
    el("p", { class: "muted auth-alt" },
      el("a", { href: "#/login" }, "← Retour à la connexion"))));
  emailInput.focus();
}

// ── Nouveau mot de passe (depuis le jeton de l'URL) ──────────────────────────

export function renderReset(root, token) {
  const errBox = el("div", { class: "errbox hidden" });
  const okBox = el("div", { class: "okbox hidden" });
  const passInput = el("input", { type: "password", name: "password",
    required: true, autocomplete: "new-password", minlength: "8" });
  const confirmInput = el("input", { type: "password", name: "confirm",
    required: true, autocomplete: "new-password", minlength: "8" });
  const submitBtn = el("button", { class: "btn btn-primary btn-block", type: "submit" },
    "Réinitialiser mon mot de passe");

  function showError(m) { errBox.textContent = m; errBox.classList.remove("hidden"); }

  async function onSubmit(e) {
    e.preventDefault();
    errBox.classList.add("hidden");
    const password = passInput.value;
    if (password.length < 8)
      return showError("Le mot de passe doit contenir au moins 8 caractères.");
    if (password !== confirmInput.value)
      return showError("Les deux mots de passe ne correspondent pas.");
    submitBtn.disabled = true;
    submitBtn.textContent = "Un instant…";
    try {
      const r = await api.resetPassword(token, password);
      form.classList.add("hidden");
      okBox.textContent = (r && r.message) || "Votre mot de passe a été réinitialisé.";
      okBox.classList.remove("hidden");
      // Retour à la connexion après un court instant.
      setTimeout(() => navigate("#/login"), 2200);
    } catch (err) {
      showError(err instanceof ApiError ? err.message : "Une erreur est survenue.");
      submitBtn.disabled = false;
      submitBtn.textContent = "Réinitialiser mon mot de passe";
    }
  }

  const form = el("form", { onSubmit },
    el("div", { class: "field" },
      el("label", {}, "Nouveau mot de passe"),
      el("div", { class: "help" }, "Au moins 8 caractères."),
      passInput),
    el("div", { class: "field" },
      el("label", {}, "Confirmer le mot de passe"), confirmInput),
    errBox,
    el("div", { style: { marginTop: "8px" } }, submitBtn));

  mount(root, authCard(
    el("h1", { class: "auth-title" }, "Nouveau mot de passe"),
    okBox, form,
    el("p", { class: "muted auth-alt" },
      el("a", { href: "#/forgot" }, "Demander un nouveau lien"),
      " · ",
      el("a", { href: "#/login" }, "Connexion"))));
  passInput.focus();
}

// ── Vérification d'email (clic sur le lien reçu par email) ───────────────────

export function renderVerify(root, token) {
  const status = el("p", { class: "muted", style: { textAlign: "center" } },
    "Vérification de votre adresse en cours…");
  // Destination selon l'état de session (connecté → tableau de bord).
  const nextHref = getToken() ? "#/properties" : "#/login";
  const nextLabel = getToken() ? "Aller à mes logements" : "Se connecter";
  const link = el("p", { class: "muted auth-alt hidden" },
    el("a", { href: nextHref }, nextLabel));

  mount(root, authCard(
    el("h1", { class: "auth-title" }, "Vérification de l'email"),
    status, link));

  (async () => {
    try {
      const r = await api.verifyEmail(token);
      status.className = "okbox";
      status.textContent = (r && r.message) || "Votre adresse email est confirmée.";
      // Si une session est active, rafraîchir le profil pour masquer le bandeau.
      if (getToken()) { try { setOwner(await api.me()); } catch (_) { /* non bloquant */ } }
    } catch (err) {
      status.className = "errbox";
      status.textContent = err instanceof ApiError ? err.message
        : "Impossible de vérifier votre adresse.";
    }
    link.classList.remove("hidden");
  })();
}
