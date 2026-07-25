/* Écran de connexion / inscription (§3.1).
   Erreurs applicatives affichées en français (email déjà pris, identifiants
   erronés). Le jeton est stocké par app.js après succès. */

import { api, suppressUnauthorizedRedirect } from "../api.js";
import { setToken, setOwner } from "../store.js";
import { el, icon, mount, refreshIcons, toast } from "../ui.js";
import { navigate } from "../nav.js";
import { redirect } from "../redirect.js";
import { submitAuth } from "../authflow.js";

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

  // Choix de l'offre à l'inscription (V2-05a/b) : offre gratuite présélectionnée.
  // Les offres payantes sont désormais **souscriptibles** — après création du
  // compte, on redirige vers le Checkout Stripe du plan choisi (V2-05b).
  const plansBox = el("div", { class: "signup-plans" });
  let plansLoaded = false;

  function syncChoiceStyles() {
    plansBox.querySelectorAll(".plan-choice").forEach((lbl) => {
      const input = lbl.querySelector("input");
      lbl.classList.toggle("on", input && input.checked);
    });
  }

  async function loadPlans() {
    if (plansLoaded) return;
    plansLoaded = true;
    try {
      // Chooser (V2-18a) : essai en tête (présélectionné), offres payantes
      // souscriptibles. Le gratuit historique ne figure PAS à l'inscription.
      const plans = (await api.listPlans()).filter((p) => p.id !== "free");
      const choices = el("div", { class: "plan-choices" }, ...plans.map(planChoice));
      choices.addEventListener("change", syncChoiceStyles);
      mount(plansBox,
        el("div", { class: "label" }, "Votre offre"),
        choices,
        el("div", { class: "help" },
          "Démarrez par un essai gratuit de 21 jours — aucune carte requise. "
          + "Vous pouvez aussi souscrire une offre payante dès maintenant (paiement "
          + "sécurisé Stripe après la création du compte). Le watermark "
          + "« Créé avec Holaguia » figure sur l'essai et l'offre Solo ; l'offre "
          + "Pro est en marque blanche."));
      syncChoiceStyles();
      refreshIcons();
    } catch (_) { /* non bloquant : l'inscription reste possible sur l'essai par défaut */ }
  }

  function euros(cts) {
    if (!cts) return "Gratuit";
    const n = cts / 100;
    return (Number.isInteger(n) ? String(n) : n.toFixed(2).replace(".", ",")) + " €/mois";
  }

  function planChoice(p) {
    const trial = p.id === "trial";
    const label = trial ? "Essai gratuit — 21 jours" : p.name;
    const priceText = trial ? "toutes les fonctionnalités" : euros(p.price_month_cts);
    return el("label", { class: "plan-choice" + (trial ? " featured" : "") },
      el("input", { type: "radio", name: "plan", value: p.id, ...(trial ? { checked: true } : {}) }),
      el("span", { class: "pc-body" },
        el("b", {}, label), " ",
        el("span", { class: "pc-price" }, priceText)));
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

    const chosen = fields.querySelector('input[name="plan"]:checked');
    const planId = mode === "register" && chosen ? chosen.value : "trial";
    const credentials = mode === "register"
      ? { email, password, full_name: nameF.input.value.trim(),
          company_name: companyF.input.value.trim() || null,
          phone: phoneF.input.value.trim() || null }
      : { email, password };

    // Tout l'enchaînement (création → jeton → /me → Checkout) est encadré par
    // `submitAuth` : session posée/vérifiée avant le paiement, échec Checkout →
    // « Mon abonnement » connecté, aucun 401 n'éjecte (route publique, V2-16).
    const result = await submitAuth({
      mode, planId, credentials,
      deps: {
        register: api.register, login: api.login, me: api.me,
        startCheckout: api.startCheckout,
        setToken, setOwner, redirect, navigate, toast,
        suppress: suppressUnauthorizedRedirect,
      },
    });

    if (result.outcome === "error") {
      showError(result.message);
      submitBtn.disabled = false;
      renderFields();
    }
    // "redirect" / "properties" / "abonnement" : la navigation a déjà eu lieu.
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
