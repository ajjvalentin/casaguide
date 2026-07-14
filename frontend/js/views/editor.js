/* Éditeur de guide (M-03) + repositionnement du logement (M-05).

   Navigation par chapitres A→I avec complétude par chapitre ; pour chaque
   section, formulaire généré depuis field_schema (voir components/dynform.js),
   sauvegarde par section (bouton + Cmd/Ctrl+S), visibilité et état « complété »,
   champs secrets chiffrés. Bandeau d'alerte + éditeur de position sur carte si
   le géocodage n'est pas au niveau « rooftop ». */

import { api } from "../api.js";
import {
  el, icon, mount, clear, t, toast, openModal, confirmDialog, loadingBlock, refreshIcons,
} from "../ui.js";
import { navigate } from "../nav.js";
import { CHAPTER_ORDER, chapterMeta } from "../constants.js";
import { buildSectionForm } from "../components/dynform.js";
import { buildMediaPanel } from "../components/media.js";
import { runEnrichment } from "./properties.js";

const ACCURACY_LABEL = { rooftop: "précise", street: "au niveau de la rue", city: "au centre de la commune" };

// Groupe distinct des sections « équipe d'entretien » (audience='staff', M-13).
const STAFF_META = { name: "Équipe d'entretien", icon: "clipboard-list", color: "#5B6B75" };
const isStaff = (s) => s.audience === "staff";

export async function renderEditor(view, pid) {
  mount(view, el("div", { class: "page" }, loadingBlock("Ouverture de l'éditeur…")));

  let property, sectionsResp, secrets = {}, secretsAvailable = true;
  try {
    [property, sectionsResp] = await Promise.all([api.getProperty(pid), api.listSections(pid)]);
  } catch (err) {
    return mount(view, el("div", { class: "page" },
      el("div", { class: "errbox" }, err.message || "Logement introuvable.")));
  }
  try {
    secrets = (await api.getSecrets(pid)) || {};
  } catch (err) {
    secretsAvailable = false; // CASAGUIDE_SECRET_KEY absente (503) → 5.x non bloquant
  }

  const sections = sectionsResp.sections.map((s) => ({
    ...s,
    is_visible: s.is_visible == null ? true : s.is_visible,
    completed: !!s.completed,
  }));
  const byCode = new Map(sections.map((s) => [s.code, s]));
  let current = sections[0]?.code;
  const expanded = new Set();

  // ── Ossature de la page ───────────────────────────────────────────────────
  const globalMeter = el("div", { class: "meter", style: { maxWidth: "260px" } }, el("i"));
  const globalPct = el("b", {});
  const statusBadge = el("span", { class: "badge badge-" + property.status });
  const headerRight = el("div", { class: "row" });
  let translationBtn = null;   // bouton « Traductions » (M-09), (re)créé au rendu
  const banner = el("div", {});
  const sidebar = el("nav", { class: "card chapters", style: { padding: "10px" } });
  const panel = el("section", { class: "card section-panel" });

  const page = el("div", { class: "page" },
    el("div", { class: "crumbs" },
      el("a", { href: "#/properties" }, "Mes logements"), icon("chevron-right", 14),
      el("span", {}, property.name)),
    el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "flex-start", marginBottom: "6px" } },
      el("div", {}, el("h1", { class: "page-title", style: { margin: "0 0 6px" } }, property.name),
        el("div", { class: "row", style: { gap: "10px" } }, statusBadge, globalPct,
          el("span", { class: "muted", style: { fontSize: "13px" } }, "complété"))),
      headerRight),
    globalMeter,
    banner,
    el("div", { class: "editor", style: { marginTop: "18px" } }, sidebar, panel));
  mount(view, page);

  renderHeaderActions();
  renderBanner();
  refreshMeter();
  if (current) { expanded.add(byCode.get(current).chapter); selectSection(current); }
  renderSidebar();

  // Cmd/Ctrl+S : sauvegarde la section active (nettoie l'ancien handler éventuel)
  if (window._casaSaveHandler) document.removeEventListener("keydown", window._casaSaveHandler);
  window._casaSaveHandler = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      if (!document.contains(panel)) { document.removeEventListener("keydown", window._casaSaveHandler); return; }
      e.preventDefault(); saveCurrent();
    }
  };
  document.addEventListener("keydown", window._casaSaveHandler);

  // ── Rendu de la barre latérale (chapitres) ────────────────────────────────
  function renderSidebar() {
    clear(sidebar);
    for (const ch of CHAPTER_ORDER) {
      const chSecs = sections.filter((s) => s.chapter === ch);
      if (!chSecs.length) continue;
      const meta = chapterMeta(ch);
      const done = chSecs.filter((s) => s.completed).length;
      const isOpen = expanded.has(ch);

      const head = el("button", { class: "chap-head", onClick: () => { isOpen ? expanded.delete(ch) : expanded.add(ch); renderSidebar(); } },
        el("span", { class: "chap-dot", style: { background: meta.color } }, icon(meta.icon, 15)),
        el("span", { class: "nm" }, meta.name),
        el("span", { class: "cnt" }, `${done}/${chSecs.length}`),
        icon(isOpen ? "chevron-down" : "chevron-right", 15));
      const chapNode = el("div", { class: "chap" }, head);

      if (isOpen) {
        const listEl = el("div", { class: "sec-list" });
        for (const s of chSecs) {
          const link = el("button", {
            class: "sec-link" + (s.code === current ? " on" : ""),
            onClick: () => selectSection(s.code),
          },
            el("span", { class: s.is_visible ? "" : "dim" }, t(s.name_i18n, s.code)),
            s.is_sensitive ? icon("lock", 12) : null,
            icon("circle-check", 15, s.completed) );
          // Marque de complétude
          const tick = link.querySelector("[data-lucide]:last-child");
          if (tick) { tick.classList.add("tick"); if (!s.completed) tick.classList.add("off"); }
          listEl.append(link);
        }
        chapNode.append(listEl);
      }
      sidebar.append(chapNode);
    }

    // Groupe distinct « Équipe d'entretien » (sections staff, M-13) — jamais
    // visible du voyageur ; son lien /s est affiché dans le panneau de section.
    const staffSecs = sections.filter(isStaff);
    if (staffSecs.length) {
      const done = staffSecs.filter((s) => s.completed).length;
      const isOpen = expanded.has("S");
      const head = el("button", { class: "chap-head", onClick: () => { isOpen ? expanded.delete("S") : expanded.add("S"); renderSidebar(); } },
        el("span", { class: "chap-dot", style: { background: STAFF_META.color } }, icon(STAFF_META.icon, 15)),
        el("span", { class: "nm" }, STAFF_META.name),
        el("span", { class: "cnt" }, `${done}/${staffSecs.length}`),
        icon(isOpen ? "chevron-down" : "chevron-right", 15));
      const chapNode = el("div", { class: "chap" }, head);
      if (isOpen) {
        const listEl = el("div", { class: "sec-list" });
        for (const s of staffSecs) {
          const link = el("button", {
            class: "sec-link" + (s.code === current ? " on" : ""),
            onClick: () => selectSection(s.code),
          },
            el("span", { class: s.is_visible ? "" : "dim" }, t(s.name_i18n, s.code)),
            icon("circle-check", 15, s.completed));
          const tick = link.querySelector("[data-lucide]:last-child");
          if (tick) { tick.classList.add("tick"); if (!s.completed) tick.classList.add("off"); }
          listEl.append(link);
        }
        chapNode.append(listEl);
      }
      sidebar.append(chapNode);
    }
    refreshIcons();
  }

  function refreshMeter() {
    // La complétude affichée est celle du guide VOYAGEUR : le cahier de l'équipe
    // d'entretien (sections staff) a son propre décompte et ne la dilue pas.
    const guestSecs = sections.filter((s) => !isStaff(s));
    const total = guestSecs.length;
    const done = guestSecs.filter((s) => s.completed).length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    globalMeter.querySelector("i").style.width = pct + "%";
    globalPct.textContent = pct + " %";
  }

  // ── Panneau d'une section ─────────────────────────────────────────────────
  function selectSection(code) {
    current = code;
    const sec = byCode.get(code);
    const meta = isStaff(sec) ? STAFF_META : chapterMeta(sec.chapter);

    const form = buildSectionForm(sec, { secrets, propertyId: pid });

    const visibleSwitch = el("input", { type: "checkbox" });
    visibleSwitch.checked = sec.is_visible;
    const completedSwitch = el("input", { type: "checkbox" });
    completedSwitch.checked = sec.completed;
    const saveBtn = el("button", { class: "btn btn-primary" }, icon("save", 17), "Enregistrer");

    const secretUnavailable = form.hasSecrets && !secretsAvailable
      ? el("div", { class: "notice notice-warn", style: { marginBottom: "16px" } }, icon("triangle-alert", 18),
        el("div", {}, "Le stockage sécurisé n'est pas configuré sur le serveur (clé CASAGUIDE_SECRET_KEY) : les champs chiffrés ne pourront pas être enregistrés."))
      : null;

    mount(panel,
      el("div", { class: "sp-head" },
        el("span", { class: "chap-dot", style: { background: meta.color, width: "30px", height: "30px" } }, icon(sec.icon || meta.icon, 17)),
        el("div", {}, el("h2", {}, t(sec.name_i18n, sec.code)),
          el("div", { class: "row", style: { gap: "8px", marginTop: "4px" } },
            sec.is_sensitive ? el("span", { class: "badge badge-secret" }, icon("lock", 12), "Sensible") : null,
            sec.ai_enrichable ? el("span", { class: "badge badge-ai" }, icon("sparkles", 12), "Pré-remplissable IA") : null))),
      el("p", { class: "sp-desc" }, t(sec.description_i18n, "")),
      isStaff(sec) ? staffLinkBanner() : null,
      secretUnavailable,
      form.node,
      buildMediaPanel({ propertyId: pid, sectionCode: sec.code }).node,
      el("div", { class: "sp-toolbar" },
        el("label", { class: "switch" }, visibleSwitch, el("span", { class: "track" }), el("span", {}, "Visible dans le guide")),
        el("label", { class: "switch" }, completedSwitch, el("span", { class: "track" }), el("span", {}, "Section complétée")),
        el("span", { class: "spacer" }),
        el("span", { class: "savehint" }, el("kbd", {}, navigator.platform.includes("Mac") ? "⌘S" : "Ctrl+S")),
        saveBtn));

    saveBtn.addEventListener("click", () => saveCurrent({ visibleSwitch, completedSwitch, form, saveBtn }));
    panel._ctx = { visibleSwitch, completedSwitch, form, saveBtn };
    renderSidebar();
  }

  async function saveCurrent(ctx = panel._ctx) {
    if (!ctx) return;
    const { visibleSwitch, completedSwitch, form, saveBtn } = ctx;
    const sec = byCode.get(current);
    const { content, body_md, hasSecrets, secretsPatch } = form.collect();
    saveBtn.disabled = true; saveBtn.textContent = "Enregistrement…";
    try {
      await api.putSection(pid, current, {
        content, body_md, is_visible: visibleSwitch.checked, completed: completedSwitch.checked,
      });
      if (hasSecrets && secretsAvailable) {
        Object.assign(secrets, secretsPatch);
        await api.putSecrets(pid, {
          wifi_ssid: secrets.wifi_ssid || null, wifi_pass: secrets.wifi_pass || null,
          keybox_code: secrets.keybox_code || null, keybox_notes: secrets.keybox_notes || null,
        });
      } else if (hasSecrets && !secretsAvailable) {
        toast("Section enregistrée (champs chiffrés ignorés : stockage non configuré).", "err");
      }
      // Mise à jour de l'état local
      sec.content = content; sec.body_md = body_md;
      sec.is_visible = visibleSwitch.checked; sec.completed = completedSwitch.checked;
      if (!(hasSecrets && !secretsAvailable)) toast("Section enregistrée.", "ok");
      refreshMeter(); renderSidebar();
    } catch (err) {
      toast(err.message || "Enregistrement impossible.", "err");
    } finally {
      saveBtn.disabled = false; mount(saveBtn, icon("save", 17), "Enregistrer");
    }
  }

  // ── Actions d'en-tête (publier, voir le guide) ────────────────────────────
  function renderHeaderActions() {
    statusBadge.textContent = { draft: "Brouillon", published: "Publié", archived: "Archivé" }[property.status] || property.status;
    statusBadge.className = "badge badge-" + property.status;
    clear(headerRight);
    if (property.status === "published") {
      // ── Traductions du guide (M-09) : bouton « Mettre à jour les traductions »
      // avec état (à jour / X éléments périmés). La (re)traduction est déclenchée
      // à la publication ; ce bouton rafraîchit après des modifications de contenu.
      translationBtn = el("button", { class: "btn btn-sm", onClick: () => runTranslate() },
        icon("languages", 16), el("span", { class: "tr-label" }, "Traductions"));
      headerRight.append(
        el("a", { class: "btn btn-sm", href: `/g/${property.guide_token}`, target: "_blank", rel: "noopener" },
          icon("external-link", 16), "Voir le guide"),
        el("button", { class: "btn btn-sm", onClick: () => copyGuideLink() },
          icon("link", 16), "Copier le lien"),
        translationBtn,
        el("button", { class: "btn btn-sm", onClick: () => downloadPoster() },
          icon("qr-code", 16), "QR à imprimer"),
        el("button", { class: "btn btn-sm", onClick: () => setStatus("draft") }, "Dépublier"));
      refreshTranslationState();
    } else {
      headerRight.append(
        el("button", { class: "btn btn-sm btn-primary", onClick: () => publish() }, icon("globe", 16), "Publier le guide"));
    }
    refreshIcons();
  }

  async function refreshTranslationState() {
    if (!translationBtn) return;
    try {
      const st = await api.translationStatus(pid);
      const label = translationBtn.querySelector(".tr-label");
      translationBtn.classList.remove("btn-warn");
      translationBtn.disabled = false;
      if (!st.total) {
        label.textContent = "Traductions";
        translationBtn.title = "Aucun texte à traduire pour le moment.";
      } else if (st.up_to_date) {
        label.textContent = "Traductions à jour";
        translationBtn.title = "Toutes les langues du guide sont à jour.";
      } else {
        label.textContent = `${st.outdated} à traduire`;
        translationBtn.classList.add("btn-warn");
        translationBtn.title = "Des contenus ont changé depuis la dernière traduction.";
      }
    } catch (_) { /* non bloquant */ }
  }

  async function runTranslate() {
    const label = translationBtn.querySelector(".tr-label");
    translationBtn.disabled = true;
    label.textContent = "Traduction…";
    try {
      await api.translate(pid);
      // La traduction s'exécute en tâche de fond : on sonde l'état quelques fois.
      for (let i = 0; i < 8; i++) {
        await new Promise((r) => setTimeout(r, 1200));
        const st = await api.translationStatus(pid);
        if (st.up_to_date) break;
      }
      toast("Traductions mises à jour.", "ok");
    } catch (err) {
      toast(err.message || "Traduction impossible.", "err");
    } finally {
      translationBtn.disabled = false;
      refreshTranslationState();
      refreshIcons();
    }
  }

  function guideLink() { return location.origin + `/g/${property.guide_token}`; }
  function staffLink() { return location.origin + `/s/${property.staff_token}`; }

  async function copyGuideLink() {
    try { await navigator.clipboard.writeText(guideLink()); toast("Lien du guide copié.", "ok"); }
    catch (_) { toast("Copie impossible — copiez le lien manuellement.", "err"); }
  }

  // Bandeau du lien /s (cahier équipe d'entretien) affiché sur une section staff.
  function staffLinkBanner() {
    const input = el("input", { type: "text", value: staffLink(), readonly: true, onFocus: (e) => e.target.select() });
    const copy = el("button", { class: "btn btn-sm", type: "button", onClick: async () => {
      try { await navigator.clipboard.writeText(staffLink()); toast("Lien du cahier copié.", "ok"); }
      catch (_) { toast("Copie impossible.", "err"); }
    } }, icon("link", 15), "Copier");
    return el("div", { class: "notice notice-info", style: { marginBottom: "16px", alignItems: "flex-start" } },
      icon("clipboard-list", 18),
      el("div", { style: { flex: "1" } },
        el("b", {}, "Lien du cahier de préparation"),
        el("div", { class: "muted", style: { fontSize: "12.5px", margin: "3px 0 8px" } },
          "À partager avec votre équipe d'entretien uniquement. Accessible même avant publication ; ne contient jamais le wifi, la boîte à clés, ni la carte des lieux."),
        el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } }, input, copy,
          el("a", { class: "btn btn-sm", href: `/s/${property.staff_token}`, target: "_blank", rel: "noopener" }, icon("external-link", 15), "Ouvrir"))));
  }

  // Téléchargement de l'affiche QR imprimable (M-07). Le PDF est protégé (owner) :
  // on le récupère avec le jeton puis on déclenche le téléchargement local.
  async function downloadPoster(size) {
    try {
      const blob = await api.posterBlob(pid, size);
      const url = URL.createObjectURL(blob);
      const a = el("a", { href: url, download: `casaguide-qr-${property.name || pid}.pdf` });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      toast("Affiche QR téléchargée.", "ok");
    } catch (err) { toast(err.message || "Génération du PDF impossible.", "err"); }
  }

  async function publish() {
    if (!(await confirmDialog("Rendre ce guide accessible via son lien public ? Les voyageurs pourront le consulter.",
      { title: "Publier le guide", okLabel: "Publier" }))) return;
    await setStatus("published");
    const link = location.origin + `/g/${property.guide_token}`;
    openModal({
      title: "Guide publié 🎉",
      body: el("div", {},
        el("p", { class: "muted", style: { marginTop: 0 } }, "Partagez ce lien (ou son QR code) avec vos voyageurs :"),
        el("div", { class: "field" }, el("input", { type: "text", value: link, readonly: true, onFocus: (e) => e.target.select() })),
        el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } },
          el("a", { class: "btn btn-sm", href: `/g/${property.guide_token}`, target: "_blank", rel: "noopener" }, icon("external-link", 16), "Ouvrir le guide"),
          el("button", { class: "btn btn-sm", type: "button", onClick: () => downloadPoster() }, icon("qr-code", 16), "QR code à imprimer"))),
      footer: [el("button", { class: "btn btn-primary", onClick: (e) => { navigator.clipboard?.writeText(link); toast("Lien copié.", "ok"); } }, "Copier le lien")],
    });
  }

  async function setStatus(status) {
    try {
      const updated = await api.updateProperty(pid, { status });
      property = { ...property, ...updated };
      renderHeaderActions();
      toast(status === "published" ? "Guide publié." : "Guide repassé en brouillon.", "ok");
    } catch (err) { toast(err.message || "Action impossible.", "err"); }
  }

  // ── Bandeau de position (M-05) ────────────────────────────────────────────
  function renderBanner() {
    clear(banner);
    if (property.lat == null) {
      banner.append(el("div", { class: "notice notice-warn", style: { marginTop: "14px" } }, icon("map-pin-off", 18),
        el("div", {}, el("b", {}, "Logement non localisé. "),
          "Lancez l'enrichissement pour géocoder l'adresse, ou placez le point manuellement. ",
          el("a", { href: "#", onClick: (e) => { e.preventDefault(); runEnrichment(pid, "initial", { onFinished: () => reloadProperty() }); } }, "Enrichir maintenant"))));
      return;
    }
    if (property.geocode_accuracy && property.geocode_accuracy !== "rooftop") {
      const btn = el("button", { class: "btn btn-sm", onClick: () => openPositionModal() }, icon("move", 16), "Ajuster la position");
      banner.append(el("div", { class: "notice notice-warn", style: { marginTop: "14px", alignItems: "center" } }, icon("map-pin", 18),
        el("div", { style: { flex: "1" } }, el("b", {}, "Position approximative "),
          `(localisation ${ACCURACY_LABEL[property.geocode_accuracy] || property.geocode_accuracy}). `,
          "Ajustez le point exact sur la carte pour des distances fiables."),
        btn));
    }
    refreshIcons();
  }

  async function reloadProperty() {
    try { property = await api.getProperty(pid); renderBanner(); renderHeaderActions(); } catch (_) {}
  }

  function openPositionModal() {
    const mapEl = el("div", { id: "pos-map" });
    const coordLine = el("div", { class: "muted", style: { fontSize: "12.5px", marginTop: "8px" } });
    const saveBtn = el("button", { class: "btn btn-primary" }, "Enregistrer la position");
    const modal = openModal({
      title: "Position du logement", size: "lg",
      body: el("div", {},
        el("p", { class: "muted", style: { marginTop: 0 } }, "Faites glisser le marqueur (ou cliquez sur la carte) pour placer précisément l'entrée du logement."),
        mapEl, coordLine),
      footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler"), saveBtn],
    });

    let lat = property.lat, lon = property.lon;
    const map = L.map(mapEl).setView([lat, lon], 16);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: "© OpenStreetMap" }).addTo(map);
    const marker = L.marker([lat, lon], { draggable: true }).addTo(map);
    const showCoords = () => { coordLine.textContent = `Latitude ${lat.toFixed(6)}, longitude ${lon.toFixed(6)}`; };
    showCoords();
    marker.on("dragend", () => { const p = marker.getLatLng(); lat = p.lat; lon = p.lng; showCoords(); });
    map.on("click", (e) => { lat = e.latlng.lat; lon = e.latlng.lng; marker.setLatLng(e.latlng); showCoords(); });
    setTimeout(() => map.invalidateSize(), 60);

    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true; saveBtn.textContent = "Enregistrement…";
      try {
        const updated = await api.updateProperty(pid, { lat, lon });
        property = { ...property, ...updated };
        modal.close();
        renderBanner();
        toast("Position mise à jour.", "ok");
        if (await confirmDialog("Recalculer les distances de tous les lieux suggérés depuis la nouvelle position ?",
          { title: "Recalculer les distances", okLabel: "Recalculer" })) {
          try {
            const r = await api.recomputeDistances(pid);
            toast(`Distances recalculées pour ${r.updated} lieu(x).`, "ok");
          } catch (e2) { toast(e2.message || "Recalcul impossible.", "err"); }
        }
      } catch (err) {
        toast(err.message || "Enregistrement impossible.", "err");
        saveBtn.disabled = false; saveBtn.textContent = "Enregistrer la position";
      }
    });
  }
}
