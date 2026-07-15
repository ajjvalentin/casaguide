/* « Mes logements » (M-03 amont) : liste, complétude, création, enrichissement.
   Chaque carte affiche le statut, la complétude du guide et le décompte des POI
   en attente. Le bouton « Nouveau logement » enchaîne création → proposition
   d'enrichissement avec suivi en direct du job. */

import { api, ApiError } from "../api.js";
import {
  el, icon, mount, clear, toast, openModal, confirmDialog, loadingBlock,
  emptyBlock, refreshIcons,
} from "../ui.js";
import { navigate } from "../nav.js";
import { COUNTRIES } from "../constants.js";

const STATUS_LABEL = { draft: "Brouillon", published: "Publié", archived: "Archivé" };

export async function renderProperties(view) {
  mount(view, el("div", { class: "page" }, loadingBlock("Chargement de vos logements…")));

  let properties;
  try {
    properties = await api.listProperties();
  } catch (err) {
    return mount(view, el("div", { class: "page" },
      el("div", { class: "errbox" }, err.message || "Impossible de charger vos logements.")));
  }

  const header = el("div", { class: "row", style: { justifyContent: "space-between", marginBottom: "20px" } },
    el("div", {},
      el("div", { class: "eyebrow" }, "Espace propriétaire"),
      el("h1", { class: "page-title", style: { margin: "2px 0 0" } }, "Mes logements")),
    el("button", { class: "btn btn-primary", onClick: () => openCreateModal() },
      icon("plus", 18), "Nouveau logement"));

  const grid = el("div", { class: "prop-grid" });
  const page = el("div", { class: "page" }, header, grid);
  mount(view, page);

  if (!properties.length) {
    mount(grid, emptyBlock({
      icon: "home", title: "Aucun logement pour le moment",
      text: "Créez votre premier logement : saisissez son adresse et laissez l'IA pré-remplir l'environnement (commerces, santé, restaurants…).",
      action: el("button", { class: "btn btn-primary", onClick: () => openCreateModal() },
        icon("plus", 18), "Créer un logement"),
    }));
    return;
  }

  for (const p of properties) grid.append(propertyCard(p));
  refreshIcons();

  // Complétude + POI en attente (chargés en parallèle, non bloquants)
  properties.forEach(async (p) => {
    try {
      const s = await api.stats(p.id);
      const card = grid.querySelector(`[data-pid="${p.id}"]`);
      if (card) fillStats(card, p, s);
    } catch (_) { /* la carte reste utilisable sans indicateurs */ }
  });

  function propertyCard(p) {
    const card = el("div", { class: "card prop-card", dataset: { pid: p.id } },
      el("div", {},
        el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "flex-start" } },
          el("h3", {}, p.name),
          el("span", { class: "badge badge-" + p.status }, STATUS_LABEL[p.status] || p.status)),
        el("div", { class: "addr" }, [p.address_line1, p.postal_code, p.city].filter(Boolean).join(", "))),
      el("div", { class: "stats-slot muted", style: { fontSize: "13px" } }, "…"),
      el("div", { class: "actions" },
        el("button", { class: "btn btn-sm btn-primary", onClick: () => navigate(`#/properties/${p.id}/editor`) },
          icon("pencil-line", 16), "Compléter"),
        el("button", { class: "btn btn-sm", onClick: () => navigate(`#/properties/${p.id}/pois`) },
          icon("map-pin-check", 16), "Suggestions"),
        el("button", { class: "btn btn-sm", onClick: () => reEnrich(p) },
          icon("sparkles", 16), "Enrichir"),
        p.status === "published"
          ? el("a", { class: "btn btn-sm", href: `/g/${p.guide_token}`, target: "_blank", rel: "noopener" },
            icon("external-link", 16), "Voir le guide")
          : null,
        p.status === "published"
          ? el("button", { class: "btn btn-sm btn-ghost", "aria-label": "Copier le lien du guide", title: "Copier le lien du guide",
            onClick: () => copyGuideLink(p) }, icon("link", 16))
          : null,
        el("span", { style: { flex: "1" } }),
        el("button", { class: "btn btn-sm btn-ghost", "aria-label": "Supprimer", onClick: () => removeProperty(p) },
          icon("trash-2", 16))));
    return card;
  }

  function fillStats(card, p, s) {
    const slot = card.querySelector(".stats-slot");
    mount(slot,
      el("div", { class: "row", style: { justifyContent: "space-between", marginBottom: "5px", fontSize: "12.5px" } },
        el("span", { class: "muted" }, "Complétude du guide"),
        el("b", {}, s.completion_pct + " %")),
      el("div", { class: "meter mini" }, el("i", { style: { width: s.completion_pct + "%" } })),
      el("div", { class: "poi-counts" },
        s.pois_suggested ? el("span", { class: "badge badge-suggested" }, s.pois_suggested + " à valider") : null,
        s.pois_approved || s.pois_edited
          ? el("span", { class: "badge badge-approved" }, (s.pois_approved + s.pois_edited) + " retenus") : null,
        !s.pois_total ? el("span", { class: "muted", style: { fontSize: "12px" } }, "Pas encore enrichi") : null));
  }

  async function copyGuideLink(p) {
    try {
      await navigator.clipboard.writeText(location.origin + `/g/${p.guide_token}`);
      toast("Lien du guide copié.", "ok");
    } catch (_) { toast("Copie impossible — ouvrez le guide pour récupérer le lien.", "err"); }
  }

  async function reEnrich(p) {
    if (!(await confirmDialog(
      "Relancer l'enrichissement IA autour de l'adresse ? Les lieux déjà validés ou rejetés ne sont jamais modifiés — seules les catégories manquantes se complètent. L'opération est décomptée du quota mensuel.",
      { title: "Enrichir l'environnement", okLabel: "Lancer l'enrichissement" }))) return;
    runEnrichment(p.id, "refresh", { view });
  }

  async function removeProperty(p) {
    if (!(await confirmDialog(`Supprimer définitivement « ${p.name} » et tout son guide ?`,
      { title: "Supprimer le logement", okLabel: "Supprimer", danger: true }))) return;
    try {
      await api.deleteProperty(p.id);
      toast("Logement supprimé.", "ok");
      renderProperties(view);
    } catch (err) { toast(err.message || "Suppression impossible.", "err"); }
  }

  // ── Création d'un logement ────────────────────────────────────────────────
  function openCreateModal() {
    const f = (label, name, opts = {}) => {
      const input = el("input", { type: opts.type || "text", name, ...(opts.attrs || {}) });
      return { input, node: el("div", { class: "field" + (opts.half ? "" : "") },
        el("label", {}, label), input) };
    };
    const name = f("Nom du logement", "name", { attrs: { required: true, placeholder: "Villa Mar Azul" } });
    const addr1 = f("Adresse", "address_line1", { attrs: { required: true, placeholder: "Calle Ejemplo 1" } });
    const addr2 = f("Complément d'adresse", "address_line2");
    const postal = f("Code postal", "postal_code");
    const city = f("Ville", "city", { attrs: { required: true } });
    const region = f("Région", "region");

    const countrySel = el("select", { name: "country_code", required: true },
      ...COUNTRIES.map(([code, nm]) => el("option", { value: code }, `${nm} (${code})`)));
    countrySel.value = "ES";
    const country = el("div", { class: "field" }, el("label", {}, "Pays"), countrySel);

    const err = el("div", { class: "errbox hidden" });
    const submit = el("button", { class: "btn btn-primary" }, "Créer le logement");

    const body = el("form", { onSubmit: onCreate },
      name.node, addr1.node, addr2.node,
      el("div", { class: "grid-2" }, postal.node, city.node),
      el("div", { class: "grid-2" }, region.node, country),
      el("div", { class: "help", style: { marginTop: "-4px" } },
        "L'adresse sert à localiser le logement et à suggérer l'environnement. Vous pourrez ajuster le point exact ensuite."),
      err);
    const modal = openModal({
      title: "Nouveau logement", body,
      footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler"), submit],
    });
    submit.addEventListener("click", () => body.requestSubmit());

    async function onCreate(e) {
      e.preventDefault();
      err.classList.add("hidden");
      const payload = {
        name: name.input.value.trim(),
        address_line1: addr1.input.value.trim(),
        address_line2: addr2.input.value.trim() || null,
        postal_code: postal.input.value.trim() || null,
        city: city.input.value.trim(),
        region: region.input.value.trim() || null,
        country_code: countrySel.value,
      };
      if (!payload.name || !payload.address_line1 || !payload.city) {
        err.textContent = "Nom, adresse et ville sont obligatoires."; err.classList.remove("hidden"); return;
      }
      submit.disabled = true; submit.textContent = "Création…";
      try {
        const created = await api.createProperty(payload);
        modal.close();
        toast("Logement créé.", "ok");
        renderProperties(view);
        proposeEnrichment(created);
      } catch (e2) {
        err.textContent = e2.message || "Création impossible."; err.classList.remove("hidden");
        submit.disabled = false; submit.textContent = "Créer le logement";
      }
    }
  }

  function proposeEnrichment(property) {
    const start = el("button", { class: "btn btn-primary" }, icon("sparkles", 18), "Lancer l'enrichissement");
    const later = el("button", { class: "btn btn-ghost", type: "button" }, "Plus tard");
    const modal = openModal({
      title: "Pré-remplir l'environnement ?",
      body: el("div", {},
        el("p", { class: "muted", style: { marginTop: 0 } },
          "L'IA va localiser le logement puis suggérer commerces, santé, restaurants, transports… autour de l'adresse. Vous validerez ensuite chaque suggestion."),
        el("div", { class: "notice notice-info" }, icon("info", 18),
          el("div", {}, "Cette opération est décomptée de votre quota mensuel selon votre offre."))),
      footer: [later, start],
    });
    later.addEventListener("click", () => modal.close());
    start.addEventListener("click", () => { modal.close(); runEnrichment(property.id, "initial", { view }); });
  }
}

// ── Enrichissement avec suivi en direct (réutilisé par l'éditeur) ────────────

const ENRICH_STEPS = [
  ["geocode", "Localisation de l'adresse", "map-pin"],
  ["overpass", "Recherche des lieux à proximité", "search"],
  ["distances", "Calcul des distances", "route"],
  ["claude", "Informations locales & descriptions (IA)", "sparkles"],
];

export function runEnrichment(propertyId, trigger, { view, onFinished } = {}) {
  const statusLine = el("p", { class: "muted", style: { marginTop: 0 } }, "Démarrage…");
  const list = el("ul", { class: "steps" });
  const rows = {};
  for (const [k, label, ic] of ENRICH_STEPS) {
    const stIc = el("span", { class: "step-ic" }, icon(ic, 15));
    const sub = el("span", { class: "step-sub" });
    rows[k] = { stIc, sub };
    list.append(el("li", {}, stIc, el("span", {}, label), sub));
  }
  const foot = el("button", { class: "btn btn-ghost hidden" }, "Fermer");
  let stopped = false;
  const modal = openModal({
    title: "Enrichissement du guide", body: el("div", {}, statusLine, list),
    footer: [foot], onClose: () => { stopped = true; },
  });
  foot.addEventListener("click", () => modal.close());

  function firstPending(steps) {
    return ENRICH_STEPS.map((d) => d[0]).find((k) => !(steps[k] && steps[k].ok));
  }
  function paint(job) {
    const steps = job.steps || {};
    const cur = firstPending(steps);
    for (const [k] of ENRICH_STEPS) {
      const s = steps[k];
      const { stIc, sub } = rows[k];
      stIc.className = "step-ic";
      clear(sub);
      if (s && s.ok) { stIc.classList.add("done"); mount(stIc, icon("check", 15)); }
      else if (k === cur && job.status === "failed") { stIc.classList.add("fail"); mount(stIc, icon("x", 15)); }
      else if (k === cur && (job.status === "running" || job.status === "pending")) {
        stIc.classList.add("run");
        const sp = icon("loader-circle", 15); sp.classList.add("spin"); mount(stIc, sp);
      }
      // sous-titres informatifs
      if (k === "overpass" && s && s.pois != null) sub.textContent = s.pois + " lieux";
      if (k === "geocode" && s && s.accuracy) sub.textContent = { rooftop: "précis", street: "rue", city: "commune" }[s.accuracy] || s.accuracy;
      if (k === "claude" && s && s.cost_cts != null) sub.textContent = s.cost_cts.toFixed(2) + " ct";
    }
    refreshIcons();
  }
  function finish(job) {
    stopped = true;
    foot.classList.remove("hidden");
    if (job.status === "done") {
      const n = job.steps?.overpass?.pois || 0;
      statusLine.innerHTML = `<b style="color:var(--ok)">Terminé</b> — ${n} lieu(x) suggéré(s) à valider.`;
      const go = el("button", { class: "btn btn-primary" }, "Voir les suggestions");
      go.addEventListener("click", () => { modal.close(); navigate(`#/properties/${propertyId}/pois`); });
      foot.after(go);
      if (onFinished) onFinished(job);
      if (view) renderProperties(view); // rafraîchit les indicateurs si on est sur la liste
    } else {
      statusLine.innerHTML = `<b style="color:var(--alert)">Échec</b> — ${job.error || "l'enrichissement n'a pas abouti."}`;
      const stepsFailed = job.steps?.overpass?.failed;
      if (stepsFailed && Object.keys(stepsFailed).length) {
        statusLine.append(el("div", { class: "muted", style: { fontSize: "12.5px", marginTop: "6px" } },
          "Catégories en échec : " + Object.keys(stepsFailed).join(", ")));
      }
    }
  }

  (async () => {
    let jobId;
    try {
      const resp = await api.enrich(propertyId, trigger);
      jobId = resp.job_id;
    } catch (err) {
      stopped = true;
      foot.classList.remove("hidden");
      statusLine.innerHTML = `<b style="color:var(--alert)">Impossible de démarrer</b> — ${err.message}`;
      return;
    }
    async function tick() {
      if (stopped) return;
      try {
        const job = await api.getJob(propertyId, jobId);
        paint(job);
        if (job.status === "done" || job.status === "failed") return finish(job);
        statusLine.textContent = "Traitement en cours…";
      } catch (_) { /* on réessaie */ }
      if (!stopped) setTimeout(tick, 1500);
    }
    tick();
  })();
}
