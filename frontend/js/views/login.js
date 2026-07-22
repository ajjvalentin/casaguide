/* Écran de connexion / inscription (§3.1).
   Erreurs applicatives affichées en français (email déjà pris, identifiants
   erronés). Le jeton est stocké par app.js après succès. */

import { api, ApiError } from "../api.js";
import { setToken, setOwner } from "../store.js";
import { el, icon, mount, refreshIcons } from "../ui.js";
import { navigate } from "../nav.js";

export function renderLogin(root) {
  let mode = "login"; // 'login' | 'register'

  const errBox = el("div", { class: "errbox hidden" });
  const submitBtn = el("button", { class: "btn btn-primary btn-block", type: "submit" }, "Se connecter");
  const fields = el("div", {});

  function showError(msg) { errBox.textContent = msg; errBox.classList.remove("hidden"); }
  function hideError() { errBox.classList.add("hidden"); }

  function field(label, name, type = "text", opts = {}) {
    const input = el("input", { type, name, ...(opts.attrs || {}) });
    return { node: el("div", { class: "field" }, el("label", {}, label),
      opts.help ? el("div", { class: "help" }, opts.help) : null, input), input };
  }

  const emailF = field("Adresse email", "email", "email", { attrs: { required: true, autocomplete: "email" } });
  const passF = field("Mot de passe", "password", "password",
    { attrs: { required: true, autocomplete: "current-password" } });
  const nameF = field("Nom complet", "full_name", "text", { attrs: { autocomplete: "name" } });
  const companyF = field("Société (facultatif)", "company_name");
  const phoneF = field("Téléphone (facultatif)", "phone", "tel");

  // Choix de l'offre à l'inscription (V2-05a) : offre gratuite présélectionnée ;
  // les offres payantes sont affichées (prix depuis l'API) mais pas encore
  // souscriptibles — aucune collecte de paiement (Stripe = V2-05b).
  const plansBox = el("div", { class: "signup-plans" });
  let plansLoaded = false;

  async function loadPlans() {
    if (plansLoaded) return;
    plansLoaded = true;
    try {
      const plans = await api.listPlans();
      mount(plansBox,
        el("div", { class: "label" }, "Votre offre"),
        el("div", { class: "plan-choices" }, ...plans.map(planChoice)),
        el("div", { class: "help" },
          "L'offre gratuite vous permet de démarrer tout de suite. Le passage à "
          + "une offre payante sera disponible prochainement."));
      refreshIcons();
    } catch (_) { /* non bloquant : l'inscription reste possible sur l'offre gratuite */ }
  }

  function euros(cts) {
    if (!cts) return "Gratuit";
    const n = cts / 100;
    return (Number.isInteger(n) ? String(n) : n.toFixed(2).replace(".", ",")) + " €/mois";
  }

  function planChoice(p) {
    const free = p.price_month_cts === 0;
    return el("label", { class: "plan-choice" + (free ? " on" : " disabled") },
      el("input", {
        type: "radio", name: "plan", value: p.id,
        ...(free ? { checked: true } : { disabled: true }),
      }),
      el("span", { class: "pc-body" },
        el("b", {}, p.name), " ",
        el("span", { class: "pc-price" }, euros(p.price_month_cts)),
        free ? null : el("span", { class: "pc-soon" }, "Bientôt")));
  }

  function renderFields() {
    if (mode === "register") {
      passF.input.setAttribute("autocomplete", "new-password");
      passF.input.setAttribute("minlength", "8");
      mount(fields, nameF.node, emailF.node, passF.node, companyF.node, phoneF.node, plansBox);
      loadPlans();
    } else {
      passF.input.setAttribute("autocomplete", "current-password");
      mount(fields, emailF.node, passF.node);
    }
    forgotLink.classList.toggle("hidden", mode !== "login");
    submitBtn.textContent = mode === "register" ? "Créer mon compte" : "Se connecter";
  }

  const tabs = el("div", { class: "auth-tabs" },
    el("button", { type: "button", onClick: () => switchMode("login") }, "Connexion"),
    el("button", { type: "button", onClick: () => switchMode("register") }, "Inscription"));

  function switchMode(m) {
    mode = m; hideError();
    [...tabs.children].forEach((b, i) => b.classList.toggle("on", (i === 0) === (m === "login")));
    renderFields();
    refreshIcons();
  }

  // Lien « Mot de passe oublié ? » — visible en mode connexion uniquement (V2-08).
  const forgotLink = el("p", { class: "muted auth-alt" },
    el("a", { href: "#/forgot" }, "Mot de passe oublié ?"));

  const form = el("form", { onSubmit: onSubmit }, fields, errBox,
    el("div", { style: { marginTop: "8px" } }, submitBtn), forgotLink);

  async function onSubmit(e) {
    e.preventDefault();
    hideError();
    const email = emailF.input.value.trim();
    const password = passF.input.value;
    if (!email || !password) return showError("Renseignez votre email et votre mot de passe.");
    if (mode === "register" && password.length < 8)
      return showError("Le mot de passe doit contenir au moins 8 caractères.");
    if (mode === "register" && !nameF.input.value.trim())
      return showError("Indiquez votre nom complet.");

    submitBtn.disabled = true;
    submitBtn.textContent = "Un instant…";
    try {
      let tokenResp;
      if (mode === "register") {
        tokenResp = await api.register({
          email, password,
          full_name: nameF.input.value.trim(),
          company_name: companyF.input.value.trim() || null,
          phone: phoneF.input.value.trim() || null,
        });
      } else {
        tokenResp = await api.login({ email, password });
      }
      setToken(tokenResp.access_token);
      setOwner(await api.me());
      navigate("#/properties");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Une erreur est survenue.";
      showError(msg);
      submitBtn.disabled = false;
      renderFields();
    }
  }

  const card = el("div", { class: "card auth-card" },
    el("a", { class: "brand", href: "#/" }, el("span", { class: "mark" }, icon("map-pinned", 20)), "CasaGuide"),
    el("p", { class: "muted", style: { textAlign: "center", margin: "2px 0 0", fontSize: "14px" } },
      "Espace propriétaire — vos guides d'accueil"),
    tabs, form);

  mount(root, el("div", { class: "auth-wrap" }, card));
  switchMode("login");
  emailF.input.focus();
}
