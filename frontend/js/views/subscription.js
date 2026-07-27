/* « Mon abonnement » (#/abonnement, V2-05a puis V2-05b).

   Affiche le plan courant, les jauges d'utilisation (logements, enrichissements
   du mois, langues) et le catalogue des offres. Les prix viennent de l'API
   (`/api/plans`), jamais codés en dur.

   V2-05b : les boutons sont réels. « Passer en solo/pro » ouvre le Checkout
   Stripe (redirection) ; « Gérer mon abonnement » (visible seulement si un
   Customer Stripe existe) ouvre le portail client. L'abonnement n'est jamais
   modifié côté client : la vérité vient du webhook (bandeau ?checkout=…). */

import { api, ApiError } from "../api.js";
import { el, icon, mount, loadingBlock, refreshIcons, toast, confirmDialog } from "../ui.js";
import { redirect } from "../redirect.js";
import { handlePlanLimit } from "../quota.js";

function amount(cts) {
  const n = cts / 100;
  return (Number.isInteger(n) ? String(n) : n.toFixed(2).replace(".", ",")) + " €";
}

function euros(cts) {
  if (!cts) return "Gratuit";
  return amount(cts) + " / mois";
}

/* Total mensuel EFFECTIF d'un abonnement : prix de base + add-ons × unité
   (V2-18d, micro-cavalier) — l'en-tête « Offre actuelle » doit refléter ce que
   le client paie réellement (ex. Pro 24 € + 2 add-ons = 30 €), pas le seul prix
   de base. */
function effectiveMonthlyCts(sub) {
  const base = sub.plan.price_month_cts || 0;
  const unit = sub.plan.addon_property_price_cts || 0;
  return base + (sub.addon_qty || 0) * unit;
}

/* Un abonné payant est-il déjà actif ? (V2-18d) Un changement d'offre se fait
   alors in-place (proration Stripe) plutôt que par un nouveau Checkout — qui
   créerait un doublon d'abonnement. Miroir de `plans.has_active_paid_subscription`. */
function isPaidActive(sub) {
  return sub.plan.price_month_cts > 0
    && (sub.status === "active" || sub.status === "past_due")
    && sub.has_stripe_customer;
}

/* Un plan est-il multilingue ? Grille V2-18b : `features.langs === 'all'`
   (illimité) ou un entier > 1. Ne jamais comparer 'all' numériquement. */
function isMultilingual(f) {
  return f.langs === "all" || (Number(f.langs) || 1) > 1;
}

/* Une jauge « libellé : used / limit » + barre. limit null = illimité.
   `text` (optionnel) remplace la valeur numérique (V2-17 : « Toutes les langues
   disponibles » plutôt qu'un « X / 5 »). */
function gauge(label, g, unit = "", text = null) {
  const unlimited = g.limit == null;
  const pct = unlimited ? 0 : Math.min(100, Math.round((g.used / Math.max(1, g.limit)) * 100));
  const full = !unlimited && g.used >= g.limit;
  const value = text != null ? text
    : (unlimited ? `${g.used} · illimité` : `${g.used} / ${g.limit}${unit ? " " + unit : ""}`);
  const showBar = text == null;
  return el("div", { class: "usage-row" },
    el("div", { class: "row", style: { justifyContent: "space-between", fontSize: "13px", marginBottom: "4px" } },
      el("span", { class: "muted" }, label),
      el("b", { style: full ? { color: "var(--alert)" } : {} }, value)),
    showBar ? el("div", { class: "meter mini" },
      el("i", { style: { width: (unlimited ? 100 : pct) + "%", opacity: unlimited ? .35 : 1 } })) : null);
}

/* Jours restants d'un essai (arrondi au jour supérieur, jamais négatif). */
function trialDaysLeft(sub) {
  if (!sub.trial_ends_at) return 0;
  const ms = new Date(sub.trial_ends_at).getTime() - Date.now();
  return Math.max(0, Math.ceil(ms / 86400000));
}

/* Encart d'état d'essai : compte à rebours (en cours) ou fin d'essai (expiré). */
function trialBlock(sub) {
  if (sub.trial_expired) {
    return el("div", { class: "callout callout-warn", style: { marginBottom: "18px" } },
      icon("lock", 18),
      el("div", {},
        el("b", {}, "Votre essai est terminé."),
        el("div", { class: "muted", style: { fontSize: "13px", marginTop: "2px" } },
          "Vos guides restent en ligne (les QR déjà partagés fonctionnent). "
          + "Choisissez une offre ci-dessous pour reprendre la main sur vos "
          + "logements — aucune donnée n'est perdue.")));
  }
  if (sub.on_trial) {
    const d = trialDaysLeft(sub);
    return el("div", { class: "callout callout-info", style: { marginBottom: "18px" } },
      icon("clock", 18),
      el("div", {},
        el("b", {}, `Essai gratuit — ${d} jour${d > 1 ? "s" : ""} restant${d > 1 ? "s" : ""}.`),
        el("div", { class: "muted", style: { fontSize: "13px", marginTop: "2px" } },
          "Vous profitez de toutes les fonctionnalités. Choisissez votre offre "
          + "quand vous voulez — sans interruption de vos guides.")));
  }
  return null;
}

/* Bandeau de retour de Checkout (V2-05b) : purement informatif. L'abonnement
   n'est JAMAIS modifié par le success_url — la confirmation vient du webhook
   Stripe (seule source de vérité). On lit ?checkout=success|cancel dans le hash. */
function checkoutParam() {
  const q = (location.hash.split("?")[1] || "");
  return new URLSearchParams(q).get("checkout");
}

function checkoutBanner() {
  const status = checkoutParam();
  if (status === "success") {
    return el("div", { class: "callout callout-info", style: { marginBottom: "18px" } },
      icon("clock", 18),
      el("div", {},
        el("b", {}, "Paiement en cours de confirmation."),
        el("div", { class: "muted", style: { fontSize: "13px", marginTop: "2px" } },
          "Votre nouvelle offre s'activera dès réception de la confirmation de "
          + "Stripe (quelques secondes). Actualisez la page si besoin.")));
  }
  if (status === "cancel") {
    return el("div", { class: "callout callout-warn", style: { marginBottom: "18px" } },
      icon("info", 18),
      el("div", {},
        el("b", {}, "Paiement annulé."),
        el("div", { class: "muted", style: { fontSize: "13px", marginTop: "2px" } },
          "Votre offre n'a pas changé — aucune donnée n'a été modifiée.")));
  }
  return null;
}

function featureLine(ok, text) {
  return el("li", { class: "feat " + (ok ? "on" : "off") },
    icon(ok ? "check" : "minus", 15), el("span", {}, text));
}

function planFeatures(plan) {
  const f = plan.features || {};
  const props = plan.max_properties == null
    ? "Logements illimités"
    : `${plan.max_properties} logement${plan.max_properties > 1 ? "s" : ""} inclus`;
  // Add-on « logement supplémentaire » (V2-18b, Pro) : mentionné honnêtement.
  const addon = plan.addon_property_price_cts
    ? `${amount(plan.addon_property_price_cts)} / mois par logement supplémentaire`
    : null;
  // V2-17/V2-18b : la promesse = la capacité réelle → « Toutes les langues
  // disponibles » dès qu'un plan dépasse la langue source (jamais « Jusqu'à N »).
  const multi = isMultilingual(f);
  const langs = multi ? "Toutes les langues disponibles" : "Guide en 1 langue";
  // Guide équipe : exclusif Pro (aperçu pendant l'essai).
  const staff = f.staff_guide === true || f.staff_guide === "preview";
  const staffLabel = f.staff_guide === "preview"
    ? "Guide de l'équipe d'entretien (aperçu)"
    : "Guide de l'équipe d'entretien";
  return el("ul", { class: "feat-list" },
    featureLine(true, props),
    addon ? featureLine(true, addon) : null,
    featureLine(true, `${plan.enrich_quota} enrichissement${plan.enrich_quota > 1 ? "s" : ""} IA / mois / logement`),
    featureLine(multi, langs),
    featureLine(staff, staffLabel),
    featureLine(!!f.pdf_export, "Export PDF & guide hors-ligne"),
    featureLine(!f.watermark, f.watermark ? "Mention « Créé avec Holaguia »" : "Guide sans mention Holaguia"),
    featureLine(!!f.stats, "Statistiques de consultation"),
    featureLine(!!f.white_label, "Marque blanche complète"));
}

/* Stepper « Logements supplémentaires » (add-on Pro, V2-18b). Affiché pour un
   abonnement Pro actif : quantité courante, +/-, impact tarifaire, confirmation.
   La quantité effective revient par le webhook → on affiche « mise à jour en
   cours » et on invite à actualiser (le front ne modifie jamais l'abonnement). */
function addonStepper(sub) {
  if (sub.plan.id !== "pro" || !sub.plan.addon_property_price_cts) return null;
  const base = sub.plan.price_month_cts;
  const unit = sub.plan.addon_property_price_cts;
  const included = sub.plan.max_properties || 0;
  const initial = sub.addon_qty || 0;
  let qty = initial;

  const qtyEl = el("b", { style: { fontSize: "18px", minWidth: "28px", textAlign: "center" } }, String(qty));
  const impact = el("div", { class: "muted", style: { fontSize: "13px", marginTop: "6px" } });
  const statusEl = el("div", { class: "muted", style: { fontSize: "13px", marginTop: "8px" } });
  const minus = el("button", { class: "btn btn-ghost btn-icon", "aria-label": "Retirer" }, "−");
  const plus = el("button", { class: "btn btn-ghost btn-icon", "aria-label": "Ajouter" }, "+");
  const confirm = el("button", { class: "btn btn-primary" }, "Confirmer");

  function refresh() {
    qtyEl.textContent = String(qty);
    minus.disabled = qty <= 0;
    confirm.disabled = qty === initial;
    impact.textContent = qty === initial
      ? `Total actuel : ${euros(base + qty * unit)} (${included + qty} logements).`
      : `Votre abonnement passera à ${euros(base + qty * unit)} `
        + `(${included + qty} logements).`;
  }
  minus.addEventListener("click", () => { if (qty > 0) { qty--; refresh(); } });
  plus.addEventListener("click", () => { qty++; refresh(); });
  confirm.addEventListener("click", async () => {
    confirm.disabled = true; minus.disabled = plus.disabled = true;
    statusEl.textContent = "Mise à jour en cours…";
    try {
      await api.updateAddons(qty);
      statusEl.textContent = "Demande envoyée : la modification sera confirmée par "
        + "Stripe dans quelques secondes. Actualisez la page pour voir la nouvelle "
        + "quantité.";
      plus.disabled = false;
    } catch (err) {
      minus.disabled = qty <= 0; plus.disabled = false; refresh();
      statusEl.textContent = "";
      if (!handlePlanLimit(err)) toast(err.message || "Modification impossible.", "err");
    }
  });
  refresh();

  return el("div", { class: "card", style: { marginBottom: "22px" } },
    el("h2", { style: { margin: "0 0 4px", fontSize: "18px" } }, "Logements supplémentaires"),
    el("p", { class: "muted", style: { fontSize: "13px", margin: "0 0 12px" } },
      `Votre offre Pro inclut ${included} logements. Ajoutez-en autant que `
      + `nécessaire à ${amount(unit)} / mois chacun.`),
    el("div", { class: "row", style: { alignItems: "center", gap: "12px" } },
      minus, qtyEl, plus),
    impact,
    el("div", { style: { marginTop: "12px" } }, confirm),
    statusEl);
}

/* Lance le Checkout d'un plan payant et redirige. Gère proprement le 503
   (Stripe pas encore configuré côté serveur) sans jamais d'alert() brut. */
async function startCheckout(planId, btn) {
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Redirection…";
  try {
    const { url } = await api.startCheckout(planId);
    redirect(url);
  } catch (err) {
    btn.disabled = false; btn.textContent = prev;
    const msg = err instanceof ApiError && err.status === 503
      ? "Le paiement en ligne n'est pas encore activé. Réessayez bientôt."
      : (err.message || "Impossible de démarrer le paiement.");
    toast(msg, "err");
  }
}

/* Change l'offre d'un abonné payant DÉJÀ actif (V2-18d) : confirmation explicite
   mentionnant la proration, puis appel de /billing/change-plan (modifie
   l'abonnement Stripe existant). L'abonnement n'est jamais muté côté client — la
   vérité vient du webhook → on affiche « mise à jour en cours » et on invite à
   actualiser. */
async function startChangePlan(plan, btn) {
  const ok = await confirmDialog(
    `Votre abonnement passera immédiatement à l'offre ${plan.name} `
    + `(${euros(plan.price_month_cts)}). Le temps déjà payé sur votre offre `
    + `actuelle sera crédité automatiquement (proration Stripe).`,
    { title: "Changer d'offre", okLabel: "Confirmer le changement" });
  if (!ok) return;
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Mise à jour…";
  try {
    await api.changePlan(plan.id);
    btn.textContent = "Mise à jour en cours…";
    toast("Changement demandé : votre offre sera mise à jour dans quelques "
      + "secondes. Actualisez la page pour la voir.", "ok");
  } catch (err) {
    btn.disabled = false; btn.textContent = prev;
    if (!handlePlanLimit(err)) toast(err.message || "Changement impossible.", "err");
  }
}

function planCard(plan, sub) {
  const currentId = sub.plan.id;
  const isCurrent = plan.id === currentId;
  const isFree = plan.price_month_cts === 0;

  let btn;
  if (isCurrent) {
    btn = el("button", { class: "btn btn-block", disabled: true },
      icon("check", 16), "Votre offre");
  } else if (isFree) {
    // On ne « souscrit » pas le gratuit : on y revient via le portail (annulation).
    btn = el("button", { class: "btn btn-block btn-ghost", disabled: true },
      "Offre de départ");
  } else {
    btn = el("button", { class: "btn btn-block btn-primary" }, "Passer en " + plan.name);
    // Abonné payant actif → changement in-place (proration) ; sinon (essai/free)
    // → Checkout pour entrer dans le payant.
    btn.addEventListener("click", () =>
      isPaidActive(sub) ? startChangePlan(plan, btn) : startCheckout(plan.id, btn));
  }

  return el("div", { class: "card plan-card" + (isCurrent ? " plan-current" : "") },
    isCurrent ? el("span", { class: "badge badge-published plan-tag" }, "Offre actuelle") : null,
    el("h3", { style: { margin: "0 0 2px" } }, plan.name),
    el("div", { class: "plan-price" }, euros(plan.price_month_cts)),
    planFeatures(plan),
    btn);
}

/* Bouton « Gérer mon abonnement » → portail client Stripe. Affiché seulement si
   un Customer Stripe existe (un compte n'ayant jamais payé n'a rien à gérer). */
function manageButton() {
  const btn = el("button", { class: "btn btn-ghost" },
    icon("credit-card", 16), "Gérer mon abonnement");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const { url } = await api.openBillingPortal();
      redirect(url);
    } catch (err) {
      btn.disabled = false;
      toast(err.message || "Portail indisponible pour le moment.", "err");
    }
  });
  return btn;
}

export async function renderSubscription(view) {
  mount(view, el("div", { class: "page" }, loadingBlock("Chargement de votre abonnement…")));

  let sub, plans;
  try {
    [sub, plans] = await Promise.all([api.getSubscription(), api.listPlans()]);
  } catch (err) {
    return mount(view, el("div", { class: "page" },
      el("div", { class: "errbox" }, err.message || "Impossible de charger votre abonnement.")));
  }

  const u = sub.usage;
  const header = el("div", { style: { marginBottom: "20px" } },
    el("div", { class: "eyebrow" }, "Espace propriétaire"),
    el("h1", { class: "page-title", style: { margin: "2px 0 0" } }, "Mon abonnement"));

  // Langues : « Toutes les langues disponibles » dès que le plan dépasse 1 langue
  // (V2-17) ; sinon la jauge numérique (offre gratuite historique, 1/1).
  const langsGauge = (u.langs.limit != null && u.langs.limit <= 1)
    ? gauge("Langues du guide", u.langs)
    : gauge("Langues du guide", u.langs, "", "Toutes les langues disponibles");

  // Bloc « offre actuelle » + jauges d'utilisation
  const current = el("div", { class: "card", style: { marginBottom: "22px" } },
    el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "baseline", marginBottom: "14px" } },
      el("div", {},
        el("div", { class: "muted", style: { fontSize: "13px" } }, "Offre actuelle"),
        el("h2", { style: { margin: "2px 0 0" } }, sub.plan.name)),
      el("div", { class: "plan-price", style: { margin: 0 } }, euros(effectiveMonthlyCts(sub)))),
    gauge("Logements", u.properties),
    gauge("Enrichissements IA ce mois-ci", u.enrichments, "/ logement"),
    langsGauge,
    // Portail Stripe : seulement pour un client déjà rattaché (a déjà payé).
    sub.has_stripe_customer
      ? el("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "14px" } }, manageButton())
      : null);

  // Grille « Changer d'offre » : les offres payantes, plus le gratuit UNIQUEMENT
  // pour un compte déjà en plan free (grand-périsé) — un essai ne « descend »
  // jamais vers le gratuit, son après-essai est la lecture seule (V2-18b, constat).
  // L'essai n'est jamais une cible (on ne « choisit » pas l'essai).
  const buyable = plans.filter((p) =>
    p.price_month_cts > 0 || (p.id === "free" && sub.plan.id === "free"));
  const grid = el("div", { class: "plan-grid" }, ...buyable.map((p) => planCard(p, sub)));

  mount(view, el("div", { class: "page page-narrow" },
    header, checkoutBanner(), trialBlock(sub), current,
    addonStepper(sub),
    el("h2", { style: { fontSize: "18px", margin: "0 0 12px" } }, "Changer d'offre"),
    grid,
    el("p", { class: "muted", style: { fontSize: "13px", marginTop: "14px" } },
      "Paiement sécurisé par Stripe. Vous pouvez changer d'offre ou résilier à "
      + "tout moment depuis le portail — vos guides et vos données restent intacts.")));
  refreshIcons();
}
