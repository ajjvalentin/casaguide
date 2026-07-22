/* « Mon abonnement » (#/abonnement, V2-05a, volet 3).

   Affiche le plan courant, les jauges d'utilisation (logements, enrichissements
   du mois, langues) et le catalogue des offres. Les prix viennent de l'API
   (`/api/plans`), jamais codés en dur. Les boutons de changement de plan sont
   inactifs (« paiement disponible prochainement ») : aucune collecte de paiement
   dans cette version (Stripe = V2-05b). */

import { api } from "../api.js";
import { el, icon, mount, loadingBlock, refreshIcons } from "../ui.js";

const SOON = "Paiement disponible prochainement";

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

function planCard(plan, currentId) {
  const isCurrent = plan.id === currentId;
  const btn = isCurrent
    ? el("button", { class: "btn btn-block", disabled: true }, icon("check", 16), "Votre offre")
    : el("button", { class: "btn btn-block", disabled: true, title: SOON }, "Choisir " + plan.name);
  const card = el("div", { class: "card plan-card" + (isCurrent ? " plan-current" : "") },
    isCurrent ? el("span", { class: "badge badge-published plan-tag" }, "Offre actuelle") : null,
    el("h3", { style: { margin: "0 0 2px" } }, plan.name),
    el("div", { class: "plan-price" }, euros(plan.price_month_cts)),
    planFeatures(plan),
    btn,
    isCurrent ? null : el("div", { class: "help", style: { textAlign: "center", marginTop: "6px" } }, SOON));
  return card;
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
    gauge("Langues du guide", u.langs));

  const grid = el("div", { class: "plan-grid" }, ...plans.map((p) => planCard(p, sub.plan.id)));

  mount(view, el("div", { class: "page page-narrow" },
    header, current,
    el("h2", { style: { fontSize: "18px", margin: "0 0 12px" } }, "Changer d'offre"),
    grid,
    el("p", { class: "muted", style: { fontSize: "13px", marginTop: "14px" } },
      "Le paiement en ligne arrive bientôt. En attendant, contactez-nous pour "
      + "passer à une offre supérieure — vos guides et vos données restent intacts.")));
  refreshIcons();
}
