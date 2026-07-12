/* Validation des suggestions de POI (M-04, §5.1 étape 5).

   Liste par catégorie synchronisée avec une carte Leaflet (survol liste ↔
   surbrillance carte). Pour chaque POI : Approuver / Rejeter / Modifier (nom,
   téléphone, site, description, coup de cœur). Actions groupées par catégorie,
   filtres suggérés / approuvés / rejetés. Un POI arbitré reste modifiable :
   c'est toujours le propriétaire qui décide. */

import { api } from "../api.js";
import {
  el, icon, mount, clear, t, fmtDist, toast, openModal, confirmDialog,
  loadingBlock, emptyBlock, refreshIcons,
} from "../ui.js";
import { navigate } from "../nav.js";
import { chapterMeta } from "../constants.js";

const FILTERS = [
  ["suggested", "À valider", (p) => p.status === "suggested"],
  ["kept", "Retenus", (p) => p.status === "approved" || p.status === "edited"],
  ["rejected", "Rejetés", (p) => p.status === "rejected"],
  ["all", "Tous", () => true],
];
const STATUS_BADGE = {
  suggested: ["badge-suggested", "À valider"],
  approved: ["badge-approved", "Approuvé"],
  edited: ["badge-edited", "Modifié"],
  rejected: ["badge-rejected", "Rejeté"],
};

export async function renderPois(view, pid) {
  mount(view, el("div", { class: "page" }, loadingBlock("Chargement des suggestions…")));

  let property, pois;
  try {
    [property, pois] = await Promise.all([api.getProperty(pid), api.listPois(pid)]);
  } catch (err) {
    return mount(view, el("div", { class: "page" },
      el("div", { class: "errbox" }, err.message || "Impossible de charger les suggestions.")));
  }

  let filter = pois.some((p) => p.status === "suggested") ? "suggested" : "all";
  const markers = new Map();
  const cardsById = new Map();

  const filterBar = el("div", { class: "filters" });
  const listCol = el("div", {});
  const mapEl = el("div", { id: "poi-map" });
  const summary = el("div", { class: "muted", style: { fontSize: "13px", marginTop: "10px" } });

  const page = el("div", { class: "page" },
    el("div", { class: "crumbs" },
      el("a", { href: "#/properties" }, "Mes logements"), icon("chevron-right", 14),
      el("a", { href: `#/properties/${pid}/editor` }, property.name), icon("chevron-right", 14),
      el("span", {}, "Suggestions")),
    el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "flex-start", marginBottom: "14px" } },
      el("div", {}, el("div", { class: "eyebrow" }, "Validation des lieux"),
        el("h1", { class: "page-title", style: { margin: "2px 0 0" } }, "Suggestions à valider")),
      el("button", { class: "btn btn-sm", onClick: () => navigate(`#/properties/${pid}/editor`) },
        icon("arrow-left", 16), "Retour à l'éditeur")),
    filterBar,
    el("div", { class: "pois-layout" },
      el("div", { class: "pois-list-col" }, listCol),
      el("div", { class: "pois-map-col" }, mapEl, summary)));
  mount(view, page);

  // ── Carte ─────────────────────────────────────────────────────────────────
  const center = property.lat != null ? [property.lat, property.lon] : [0, 0];
  const map = L.map(mapEl).setView(center, property.lat != null ? 14 : 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap" }).addTo(map);
  if (property.lat != null) {
    L.marker([property.lat, property.lon], {
      icon: L.divIcon({ className: "", html: '<div class="home-pin">🏠</div>', iconAnchor: [12, 12] }),
    }).addTo(map).bindPopup("<b>Votre logement</b>");
  }
  setTimeout(() => map.invalidateSize(), 60);

  for (const p of pois) {
    if (p.lat == null) continue;
    const m = L.circleMarker([p.lat, p.lon], markerStyle(p));
    m.bindPopup(`<b>${escapeHtml(p.name)}</b><br>${t(p.category_name, p.category_code)}`);
    m.on("mouseover", () => highlightCard(p.id, true));
    m.on("mouseout", () => highlightCard(p.id, false));
    m.on("click", () => { const c = cardsById.get(p.id); if (c) c.scrollIntoView({ behavior: "smooth", block: "center" }); });
    m.addTo(map);
    markers.set(p.id, m);
  }

  function markerStyle(p) {
    const dimmed = p.status === "rejected";
    return { radius: 7, weight: 2, color: "#fff", fillColor: p.map_color || "#0E5A73",
      fillOpacity: dimmed ? 0.35 : 0.95, opacity: dimmed ? 0.5 : 1 };
  }
  function highlightMarker(id, on) {
    const m = markers.get(id); if (!m) return;
    const p = pois.find((x) => x.id === id);
    m.setStyle({ radius: on ? 11 : 7, weight: on ? 3 : 2, fillColor: on ? "#0E5A73" : (p.map_color || "#0E5A73") });
    if (on) m.bringToFront();
  }
  function highlightCard(id, on) {
    const c = cardsById.get(id); if (c) c.classList.toggle("hi", on);
  }

  // ── Filtres ───────────────────────────────────────────────────────────────
  function renderFilters() {
    clear(filterBar);
    for (const [key, label, pred] of FILTERS) {
      const n = pois.filter(pred).length;
      filterBar.append(el("button", {
        class: "chip" + (filter === key ? " on" : ""), onClick: () => { filter = key; renderFilters(); renderList(); },
      }, `${label} · ${n}`));
    }
    refreshIcons();
  }

  // ── Liste par catégorie ───────────────────────────────────────────────────
  function renderList() {
    clear(listCol); cardsById.clear();
    const pred = FILTERS.find((f) => f[0] === filter)[2];
    const shown = pois.filter(pred);

    // Résumé sur la carte
    const nSug = pois.filter((p) => p.status === "suggested").length;
    const nKept = pois.filter((p) => p.status === "approved" || p.status === "edited").length;
    const nRej = pois.filter((p) => p.status === "rejected").length;
    summary.textContent = `${nSug} à valider · ${nKept} retenus · ${nRej} rejetés`;

    // Mise à jour des marqueurs (opacité selon statut)
    for (const p of pois) { const m = markers.get(p.id); if (m) m.setStyle(markerStyle(p)); }

    if (!pois.length) {
      mount(listCol, emptyBlock({
        icon: "map-pin-off", title: "Aucune suggestion",
        text: "Lancez l'enrichissement depuis l'éditeur pour obtenir des suggestions de lieux autour du logement.",
        action: el("button", { class: "btn btn-primary", onClick: () => navigate(`#/properties/${pid}/editor`) }, "Aller à l'éditeur"),
      }));
      return;
    }
    if (!shown.length) {
      mount(listCol, emptyBlock({ icon: "check-check", title: "Rien dans ce filtre",
        text: "Aucun lieu ne correspond à ce filtre pour le moment." }));
      return;
    }

    // Groupement par catégorie (ordre déjà trié par l'API)
    const groups = [];
    const idx = new Map();
    for (const p of shown) {
      if (!idx.has(p.category_code)) { idx.set(p.category_code, groups.length); groups.push({ code: p.category_code, items: [] }); }
      groups[idx.get(p.category_code)].items.push(p);
    }

    for (const g of groups) {
      const sample = g.items[0];
      const meta = chapterMeta(sample.chapter);
      const pendingHere = g.items.filter((p) => p.status === "suggested");
      const head = el("div", { class: "cat-head" },
        el("span", { class: "cat-ic", style: { background: sample.map_color || meta.color } }, icon(sample.category_icon || "map-pin", 15)),
        el("h3", {}, t(sample.category_name, sample.category_code)),
        el("span", { class: "cnt" }, `${g.items.length}`),
        el("span", { class: "spacer" }),
        pendingHere.length > 1
          ? el("button", { class: "btn btn-sm btn-ghost", onClick: () => approveCategory(g.code, pendingHere) },
            icon("check-check", 15), "Tout approuver")
          : null);
      const groupEl = el("div", { class: "cat-group" }, head);
      for (const p of g.items) groupEl.append(poiCard(p));
      listCol.append(groupEl);
    }
    refreshIcons();
  }

  function poiCard(p) {
    const d = fmtDist(p);
    const [badgeCls, badgeLbl] = STATUS_BADGE[p.status] || ["", p.status];
    const card = el("div", {
      class: "poi-card" + (p.status === "rejected" ? " is-rejected" : ""),
      style: { borderLeftColor: p.map_color || "#0E5A73" },
      onMouseenter: () => highlightMarker(p.id, true),
      onMouseleave: () => highlightMarker(p.id, false),
    },
      el("div", { class: "dist" }, el("b", {}, String(d.n)), el("span", {}, d.u)),
      el("div", { class: "body" },
        el("div", { class: "row", style: { justifyContent: "space-between", gap: "8px" } },
          el("h4", {}, p.name),
          el("span", { class: "badge " + badgeCls }, badgeLbl)),
        p.address ? el("div", { class: "sub" }, p.address) : null,
        p.description_md ? el("p", {}, p.description_md) : null,
        p.owner_comment ? el("div", { class: "fav" }, icon("heart", 14), el("span", {}, p.owner_comment)) : null,
        el("div", { class: "poi-meta" },
          p.phone ? el("a", { href: "tel:" + p.phone }, icon("phone", 13), " ", p.phone) : null,
          p.website ? el("a", { href: p.website, target: "_blank", rel: "noopener" }, icon("globe", 13), " Site") : null),
        el("div", { class: "poi-actions" },
          actionBtn(p, "approve", "Approuver", "check", "btn-ok", p.status === "approved"),
          actionBtn(p, "reject", "Rejeter", "x", "btn-danger", p.status === "rejected"),
          el("button", { class: "btn btn-sm", onClick: () => openEdit(p) }, icon("pencil-line", 15), "Modifier"))));
    cardsById.set(p.id, card);
    return card;
  }

  function actionBtn(p, action, label, ic, cls, active) {
    return el("button", {
      class: "btn btn-sm " + (active ? cls : "btn-ghost"),
      title: label, onClick: () => doAction(p, action),
    }, icon(ic, 15), label);
  }

  async function doAction(p, action) {
    try {
      const res = action === "approve" ? await api.approvePoi(pid, p.id) : await api.rejectPoi(pid, p.id);
      p.status = res.status;
      renderFilters(); renderList();
    } catch (err) { toast(err.message || "Action impossible.", "err"); }
  }

  async function approveCategory(code, pending) {
    if (!(await confirmDialog(`Approuver les ${pending.length} suggestions restantes de cette catégorie ?`,
      { title: "Tout approuver", okLabel: "Approuver" }))) return;
    let ok = 0;
    for (const p of pending) {
      try { const res = await api.approvePoi(pid, p.id); p.status = res.status; ok++; } catch (_) {}
    }
    toast(`${ok} lieu(x) approuvé(s).`, "ok");
    renderFilters(); renderList();
  }

  function openEdit(p) {
    const f = (label, name, value, type = "text") => {
      const control = type === "textarea" ? el("textarea", {}) : el("input", { type });
      if (value != null) control.value = value;
      return { control, node: el("div", { class: "field" }, el("label", {}, label), control) };
    };
    const name = f("Nom", "name", p.name);
    const phone = f("Téléphone", "phone", p.phone, "tel");
    const website = f("Site web", "website", p.website, "url");
    const desc = f("Description", "description_md", p.description_md, "textarea");
    const fav = f("Coup de cœur (commentaire personnel)", "owner_comment", p.owner_comment, "textarea");
    const save = el("button", { class: "btn btn-primary" }, "Enregistrer");
    const modal = openModal({
      title: "Modifier le lieu", size: "lg",
      body: el("form", { onSubmit: (e) => e.preventDefault() },
        name.node,
        el("div", { class: "grid-2" }, phone.node, website.node),
        desc.node, fav.node,
        el("div", { class: "notice notice-info" }, icon("info", 18),
          el("div", {}, "Enregistrer classe ce lieu comme « Modifié » : il sera retenu dans le guide."))),
      footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler"), save],
    });
    save.addEventListener("click", async () => {
      save.disabled = true; save.textContent = "Enregistrement…";
      try {
        const body = {
          name: name.control.value.trim(),
          phone: phone.control.value.trim() || null,
          website: website.control.value.trim() || null,
          description_md: desc.control.value.trim() || null,
          owner_comment: fav.control.value.trim() || null,
        };
        const res = await api.editPoi(pid, p.id, body);
        Object.assign(p, body, { status: res.status });
        modal.close();
        toast("Lieu modifié.", "ok");
        renderFilters(); renderList();
      } catch (err) {
        toast(err.message || "Modification impossible.", "err");
        save.disabled = false; save.textContent = "Enregistrer";
      }
    });
  }

  renderFilters();
  renderList();
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
