/* « Mes logements » (M-03 amont) : liste, le fil des 7 étapes, création,
   enrichissement. Chaque carte affiche le statut, le FIL (« Étape k/7 · … »,
   V2-31 volet 2 — plus jamais le « % complété » mensonger de l'audit) et le
   décompte des POI. Le bouton « Nouveau logement » enchaîne création →
   proposition d'enrichissement avec suivi en direct du job. */

import { api, ApiError } from "../api.js";
import {
  el, icon, mount, clear, toast, openModal, confirmDialog, loadingBlock,
  emptyBlock, refreshIcons,
} from "../ui.js";
import { navigate } from "../nav.js";
import { handleQuotaError } from "../quota.js";
import { getOwner } from "../store.js";
import { COUNTRIES } from "../constants.js";
import { openPropertyInfoModal } from "../components/propertyinfo.js";
import { openSendMenu } from "../components/sendmenu.js";
import { openStaffShareMenu } from "../components/staffshare.js";
import { renderJourney } from "../journey.js";
import { WELCOME_SHORT } from "./premierspas.js";

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
    // État vide = un ACCUEIL, plus une page blanche (audit §1 étape 2, branchement
    // §3 volet 3b) : le nouveau propriétaire est reçu et orienté vers les deux
    // portes — créer son premier logement, ou lire la marche à suivre.
    mount(grid, emptyBlock({
      icon: "home", title: "Bienvenue sur Holaguia",
      text: WELCOME_SHORT,
      action: el("div", { class: "row", style: { gap: "10px", justifyContent: "center", flexWrap: "wrap" } },
        el("button", { class: "btn btn-primary", onClick: () => openCreateModal() },
          icon("plus", 18), "Créer mon premier logement"),
        el("a", { class: "btn btn-ghost", href: "#/premiers-pas" },
          icon("footprints", 16), "Lire les Premiers pas")),
    }));
    return;
  }

  for (const p of properties) grid.append(propertyCard(p));
  refreshIcons();

  // Pastilles de POI (chargées en parallèle, non bloquantes). Le fil des étapes,
  // lui, est déjà rendu en synchrone depuis p.journey (aucun appel de plus).
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
      // Le fil des 7 étapes (V2-31, volet 2) : « où j'en suis + prochaine action »
      // calculé côté serveur (substance, jamais déclaration) — remplace le « % ».
      renderJourney(p.journey) || el("div", { class: "journey-slot" }),
      el("div", { class: "stats-slot muted", style: { fontSize: "13px" } }, "…"),
      // Double porte (V2-26) : le guide voyageur et le cahier d'équipe sont deux
      // produits pour deux publics → deux entrées dominantes.
      el("div", { class: "prop-doors" }, guideDoor(p), staffDoor(p)),
      // Actions secondaires (jamais dominantes). Les gestes fréquents restent
      // visibles ; le rare et destructeur (supprimer) passe dans le menu « ⋯ »
      // (V2-31). Le vocabulaire dit ce que le système FAIT (« Rechercher les
      // lieux », « Lieux suggérés »), plus jamais un jargon nu ni un sigle seul.
      el("div", { class: "actions" },
        el("button", { class: "btn btn-sm", onClick: () => navigate(`#/properties/${p.id}/calendrier`) },
          icon("calendar-days", 16), "Calendrier"),
        el("button", { class: "btn btn-sm", onClick: () => navigate(`#/properties/${p.id}/pois`) },
          icon("map-pin-check", 16), "Lieux suggérés"),
        el("button", { class: "btn btn-sm",
          title: "Holaguia repère automatiquement les lieux utiles autour de votre adresse : commerces, plages, restaurants, urgences…",
          onClick: () => reEnrich(p) },
          icon("sparkles", 16), "Rechercher les lieux"),
        p.status === "published"
          ? el("button", { class: "btn btn-sm", onClick: () => openSendMenu(p) },
            icon("send", 16), "Envoyer le guide")
          : null,
        p.status === "published"
          ? el("a", { class: "btn btn-sm", href: `/g/${p.guide_token}`, target: "_blank", rel: "noopener" },
            icon("external-link", 16), "Voir le guide")
          : null,
        // L'icône maison reçoit son libellé (plus jamais un sigle nu — principe 0.1).
        el("button", { class: "btn btn-sm btn-ghost", title: "Fiche du logement (nom, adresse, contact, position)",
          onClick: () => editInfo(p) }, icon("home", 16), "Informations"),
        el("span", { style: { flex: "1" } }),
        cardMenu(p)));
    return card;
  }

  // Menu « ⋯ » de la carte (V2-31) : abrite les gestes rares et destructeurs, à
  // commencer par la suppression du logement — elle ne vit plus à un clic de
  // « Voir le guide ». Popover ancré au bouton, refermé au clic extérieur.
  function cardMenu(p) {
    const pop = el("div", { class: "menu-pop hidden", role: "menu" },
      el("button", { class: "menu-item danger", role: "menuitem", onClick: () => { close(); removeProperty(p); } },
        icon("trash-2", 15), "Supprimer le logement…"));
    const btn = el("button", { class: "btn btn-sm btn-ghost card-menu-btn",
      "aria-label": "Plus d'actions", "aria-haspopup": "menu", "aria-expanded": "false",
      onClick: (e) => { e.stopPropagation(); toggle(); } }, icon("ellipsis", 16));
    const wrap = el("div", { class: "card-menu" }, btn, pop);
    const onAway = (ev) => { if (!wrap.contains(ev.target)) close(); };
    function close() {
      pop.classList.add("hidden"); btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("click", onAway);
    }
    function toggle() {
      const open = pop.classList.toggle("hidden") === false;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) setTimeout(() => document.addEventListener("click", onAway), 0);
      else document.removeEventListener("click", onAway);
    }
    return wrap;
  }

  // Porte « Guide Locataires » (V2-26) : ouvre l'éditeur en contexte voyageur.
  // Le menu de partage (lien multilingue) reste rattaché à cette porte.
  function guideDoor(p) {
    const main = el("button", { class: "door-main",
      onClick: () => navigate(`#/properties/${p.id}/editor`) },
      el("span", { class: "door-t" }, icon("book-open", 17), "Guide Locataires"),
      el("span", { class: "door-sub" }, "Compléter & partager le guide"));
    const share = p.status === "published"
      ? el("button", { class: "door-share", "aria-label": "Envoyer le guide", title: "Envoyer le guide (lien, QR, email, WhatsApp)",
        onClick: () => openSendMenu(p) }, icon("send", 15))
      : null;
    return el("div", { class: "door door-guide" }, main, share);
  }

  // Porte « Équipe d'entretien » (V2-26 + gating V2-18b) : ouvre l'éditeur sur le
  // groupe staff ET porte ses propres outils de transmission (lien /s/ + QR).
  //  · sans staff_access (Solo non grand-périsé) : porte VISIBLE, badgée « Pro »,
  //    clic = encart d'upsell (patron handlePlanLimit) — jamais l'accès.
  //  · essai : badge « Aperçu — inclus en Pro », accès fonctionnel.
  //  · Pro / grand-périsé : porte normale, sans badge.
  function staffDoor(p) {
    const owner = getOwner() || {};
    const access = owner.staff_access === true;
    const trial = owner.on_trial === true;

    const title = el("span", { class: "door-t" }, icon("clipboard-list", 17),
      "Équipe d'entretien", access ? null : icon("lock", 13));
    const sub = !access
      ? el("span", { class: "door-badge" }, "Pro")
      : trial
        ? el("span", { class: "door-badge" }, "Aperçu — inclus en Pro")
        : el("span", { class: "door-sub" }, "Cahier de préparation");

    const main = el("button", { class: "door-main", onClick: () => openStaffDoor(p, access) }, title, sub);
    const share = access
      ? el("button", { class: "door-share", "aria-label": "Transmettre le cahier (lien + QR)",
        title: "Lien & QR de l'équipe", onClick: () => openStaffShareMenu(p) }, icon("qr-code", 15))
      : null;
    return el("div", { class: "door door-staff" + (access ? "" : " locked") }, main, share);
  }

  // Clic sur la porte staff : accès → éditeur (contexte staff) ; sinon encart
  // d'upsell propre (même patron que les refus 402 `staff_locked` côté serveur).
  function openStaffDoor(p, access) {
    if (!access) {
      const msg = "Le cahier de l'équipe d'entretien est réservé à l'offre Pro.";
      handleQuotaError(new ApiError(402, msg, { code: "staff_locked", message: msg }));
      return;
    }
    navigate(`#/properties/${p.id}/editor/staff`);
  }

  // Fiche du logement éditable (M-24) — accessible depuis la carte.
  function editInfo(p) {
    openPropertyInfoModal(p, { onSaved: () => renderProperties(view) });
  }

  function fillStats(card, p, s) {
    // La complétude en pourcentage a disparu (V2-31 volet 2 : remplacée par le
    // fil, rendu en synchrone depuis p.journey). On ne garde ici que les pastilles
    // de POI, chargées à part car issues de l'endpoint /stats.
    const slot = card.querySelector(".stats-slot");
    mount(slot,
      el("div", { class: "poi-counts" },
        // Pastilles cliquables (V2-11) → écran « Lieux suggérés » avec le bon
        // filtre. Libellés reformulés et NEUTRES (V2-31) : une suggestion n'est
        // pas une alerte (« 96 lieux suggérés », jamais « 96 à valider » orange).
        s.pois_suggested
          ? el("a", { class: "badge badge-neutral badge-link",
            href: `#/properties/${p.id}/pois/suggested`,
            "aria-label": `Examiner les ${s.pois_suggested} lieux suggérés` },
            s.pois_suggested + " lieux suggérés")
          : null,
        s.pois_approved || s.pois_edited
          ? el("a", { class: "badge badge-approved badge-link",
            href: `#/properties/${p.id}/pois/kept`,
            "aria-label": `Voir les ${s.pois_approved + s.pois_edited} lieux dans le guide` },
            (s.pois_approved + s.pois_edited) + " lieux dans le guide")
          : null,
        !s.pois_total ? el("span", { class: "muted", style: { fontSize: "12px" } }, "Pas encore enrichi") : null));
  }

  async function reEnrich(p) {
    if (!(await confirmDialog(
      "Relancer l'enrichissement IA autour de l'adresse ? Les lieux déjà validés ou rejetés ne sont jamais modifiés — seules les catégories manquantes se complètent. L'opération est décomptée du quota mensuel.",
      { title: "Enrichir l'environnement", okLabel: "Lancer l'enrichissement" }))) return;
    runEnrichment(p.id, "refresh", { view });
  }

  async function removeProperty(p) {
    // Confirmation qui NOMME le logement et dit exactement ce qui disparaît
    // (V2-31) : l'action la plus destructrice du produit ne surprend jamais.
    if (!(await confirmDialog(
      `Le guide, les séjours et les photos de « ${p.name} » seront supprimés définitivement. Cette action est irréversible.`,
      { title: `Supprimer ${p.name} ?`, okLabel: "Supprimer le logement", danger: true }))) return;
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
        // Quota atteint (402) → encart « changez d'offre », on ferme le formulaire.
        if (handleQuotaError(e2)) { modal.close(); return; }
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
      // Quota d'enrichissement atteint (402) → encart dédié plutôt qu'un message d'échec.
      if (handleQuotaError(err)) { modal.close(); return; }
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
