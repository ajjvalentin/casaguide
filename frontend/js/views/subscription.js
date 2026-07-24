/* « Mon abonnement » (#/abonnement, V2-05a puis V2-05b).

   Affiche le plan courant, les jauges d'utilisation (logements, enrichissements
   du mois, langues) et le catalogue des offres. Les prix viennent de l'API
   (`/api/plans`), jamais codés en dur.

   V2-05b : les boutons sont réels. « Passer en solo/pro » ouvre le Checkout
   Stripe (redirection) ; « Gérer mon abonnement » (visible seulement si un
   Customer Stripe existe) ouvre le portail client. L'abonnement n'est jamais
   modifié côté client : la vérité vient du webhook (bandeau ?checkout=…). */

import { api, ApiError } from "../api.js";
import { el, icon, mount, loadingBlock, refreshIcons, toast } from "../ui.js";
import { redirect } from "../redirect.js";

function euros(cts) {
  if (!cts) return "Gratuit";
  const n = cts / 100;
  return (Number.isInteger(n) ? String(n) : n.toFixed(2).replace(".", ",")) + " € / mois";
}

/* Une jauge « libellé : used / limit » + barre. limit null = illimité. */
function gauge(label, g, unit = "") {
  const unlimited = g.limit == null;
  const pct = unlimited ? 0 : Math.min(100, Math.round((g.used / Math.max(1, g.limit)) * 100));
  const full = !unlimited && g.used >= g.limit;
  const value = unlimited ? `${g.used} · illimité` : `${g.used} / ${g.limit}${unit ? " " + unit : ""}`;
  return el("div", { class: "usage-row" },
    el("div", { class: "row", style: { justifyContent: "space-between", fontSize: "13px", marginBottom: "4px" } },
      el("span", { class: "muted" }, label),
      el("b", { style: full ? { color: "var(--alert)" } : {} }, value)),
    el("div", { class: "meter mini" },
      el("i", { style: { width: (unlimited ? 100 : pct) + "%", opacity: unlimited ? .35 : 1 } })));
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
  const props = plan.max_properties == null ? "Logements illimités" : `${plan.max_properties} logement${plan.max_properties > 1 ? "s" : ""}`;
  const langs = (f.langs || 1) <= 1 ? "Guide en 1 langue" : `Jusqu'à ${f.langs} langues par guide`;
  return el("ul", { class: "feat-list" },
    featureLine(true, props),
    featureLine(true, `${plan.enrich_quota} enrichissement${plan.enrich_quota > 1 ? "s" : ""} IA / mois / logement`),
    featureLine((f.langs || 1) > 1, langs),
    featureLine(!f.watermark, f.watermark ? "Mention « Créé avec Holaguia »" : "Guide sans mention Holaguia"),
    featureLine(!!f.stats, "Statistiques de consultation"),
    featureLine(!!f.white_label, "Marque blanche complète"));
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
    btn.addEventListener("click", () => startCheckout(plan.id, btn));
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

  // Bloc « offre actuelle » + jauges d'utilisation
  const current = el("div", { class: "card", style: { marginBottom: "22px" } },
    el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "baseline", marginBottom: "14px" } },
      el("div", {},
        el("div", { class: "muted", style: { fontSize: "13px" } }, "Offre actuelle"),
        el("h2", { style: { margin: "2px 0 0" } }, sub.plan.name)),
      el("div", { class: "plan-price", style: { margin: 0 } }, euros(sub.plan.price_month_cts))),
    gauge("Logements", u.properties),
    gauge("Enrichissements IA ce mois-ci", u.enrichments, "/ logement"),
    gauge("Langues du guide", u.langs),
    // Portail Stripe : seulement pour un client déjà rattaché (a déjà payé).
    sub.has_stripe_customer
      ? el("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "14px" } }, manageButton())
      : null);

  const grid = el("div", { class: "plan-grid" }, ...plans.map((p) => planCard(p, sub)));

  mount(view, el("div", { class: "page page-narrow" },
    header, checkoutBanner(), current,
    el("h2", { style: { fontSize: "18px", margin: "0 0 12px" } }, "Changer d'offre"),
    grid,
    el("p", { class: "muted", style: { fontSize: "13px", marginTop: "14px" } },
      "Paiement sécurisé par Stripe. Vous pouvez changer d'offre ou résilier à "
      + "tout moment depuis le portail — vos guides et vos données restent intacts.")));
  refreshIcons();
}
