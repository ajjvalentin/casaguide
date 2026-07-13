/* Zone « Photos & documents » d'une section (M-12).

   Le propriétaire illustre chaque section : ajout par bouton ou glisser-déposer,
   vignettes, légende éditable, suppression, réordonnancement (flèches + glisser).
   Les fichiers passent par l'API existante (uploadMedia) ; les vignettes sont
   récupérées avec le jeton (mediaBlobUrl → URL objet). Les images un peu lourdes
   sont réduites côté client (canvas) avant envoi, sans dépendance externe. */

import { api } from "../api.js";
import { el, icon, mount, clear, toast, confirmDialog, refreshIcons } from "../ui.js";

const ACCEPT = "image/jpeg,image/png,image/webp,application/pdf";
const MAX_MB = 10;
const CLIENT_COMPRESS_ABOVE = 4 * 1024 * 1024; // ne recompresse que les gros fichiers
const CLIENT_MAX_DIM = 2000;

/* Réduit une image trop grande via canvas (JPEG/WebP uniquement, pour préserver
   la transparence des PNG). Retourne le fichier d'origine si inutile ou en cas
   d'échec — le serveur ré-encode et retire l'EXIF de toute façon. */
async function maybeCompress(file) {
  if (!/^image\/(jpeg|webp)$/.test(file.type) || file.size < CLIENT_COMPRESS_ABOVE) return file;
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, CLIENT_MAX_DIM / Math.max(bmp.width, bmp.height));
    if (scale >= 1) { bmp.close?.(); return file; }
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close?.();
    const blob = await new Promise((res) => canvas.toBlob(res, file.type, 0.85));
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], file.name, { type: file.type });
  } catch (_) {
    return file;
  }
}

export function buildMediaPanel({ propertyId, sectionCode }) {
  let items = [];
  const objectUrls = [];
  const grid = el("div", { class: "media-grid" });
  const fileInput = el("input", {
    type: "file", accept: ACCEPT, multiple: true, style: { display: "none" },
    onChange: (e) => { addFiles(e.target.files); e.target.value = ""; },
  });
  const dropzone = el("button", {
    class: "media-drop", type: "button",
    onClick: () => fileInput.click(),
  }, icon("image-plus", 20),
    el("span", {}, "Ajouter des photos ou un PDF"),
    el("span", { class: "hint" }, "Glissez-déposez ou cliquez · JPEG, PNG, WebP, PDF · 10 Mo max"));

  ["dragover", "dragenter"].forEach((ev) => dropzone.addEventListener(ev, (e) => {
    e.preventDefault(); dropzone.classList.add("over");
  }));
  ["dragleave", "dragend"].forEach((ev) => dropzone.addEventListener(ev, () => dropzone.classList.remove("over")));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault(); dropzone.classList.remove("over");
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  });

  const node = el("div", { class: "media-panel" },
    el("div", { class: "media-head" },
      icon("images", 18), el("h3", {}, "Photos & documents"),
      el("span", { class: "media-count" })),
    el("p", { class: "media-desc" },
      "Illustrez cette section : télécommandes, boîte à clés, plan des poubelles, façade…"),
    grid, dropzone, fileInput);
  const countEl = node.querySelector(".media-count");

  load();
  return { node };

  async function load() {
    try {
      items = await api.listMedia(propertyId, sectionCode);
      render();
    } catch (err) {
      mount(grid, el("div", { class: "errbox" }, err.message || "Chargement des médias impossible."));
    }
  }

  function render() {
    objectUrls.splice(0).forEach((u) => URL.revokeObjectURL(u));
    clear(grid);
    countEl.textContent = items.length ? `${items.length}` : "";
    for (let i = 0; i < items.length; i++) grid.append(tile(items[i], i));
    refreshIcons();
  }

  function tile(m, index) {
    const thumb = el("div", { class: "media-thumb" });
    if (m.kind === "photo") {
      const img = el("img", { alt: m.caption || "Photo", loading: "lazy" });
      api.mediaBlobUrl(propertyId, m.id)
        .then((url) => { objectUrls.push(url); img.src = url; })
        .catch(() => { thumb.classList.add("broken"); mount(thumb, icon("image-off", 22)); });
      thumb.append(img);
    } else {
      thumb.classList.add("is-pdf");
      thumb.append(icon("file-text", 26), el("span", {}, "PDF"));
    }

    const caption = el("input", {
      type: "text", value: m.caption || "", placeholder: "Légende (facultatif)",
      onChange: (e) => saveCaption(m, e.target.value.trim()),
    });

    const card = el("div", { class: "media-item", draggable: "true", dataset: { id: m.id } },
      thumb,
      caption,
      el("div", { class: "media-actions" },
        el("button", { class: "btn btn-ghost btn-sm", type: "button", title: "Déplacer à gauche",
          disabled: index === 0, onClick: () => move(index, -1) }, icon("chevron-left", 15)),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", title: "Déplacer à droite",
          disabled: index === items.length - 1, onClick: () => move(index, 1) }, icon("chevron-right", 15)),
        el("span", { class: "spacer", style: { flex: "1" } }),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", title: "Supprimer",
          onClick: () => remove(m) }, icon("trash-2", 15))));

    // Glisser-déposer pour réordonner
    card.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", String(index)); card.classList.add("dragging"); });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", (e) => e.preventDefault());
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      const from = Number(e.dataTransfer.getData("text/plain"));
      if (Number.isInteger(from) && from !== index) reorder(from, index);
    });
    return card;
  }

  async function addFiles(fileList) {
    const files = Array.from(fileList || []);
    for (const file of files) {
      if (file.size > MAX_MB * 1024 * 1024) { toast(`« ${file.name} » dépasse ${MAX_MB} Mo.`, "err"); continue; }
      const form = new FormData();
      form.append("file", await maybeCompress(file));
      if (sectionCode) form.append("section_code", sectionCode);
      try {
        const created = await api.uploadMedia(propertyId, form);
        items.push(created);
        render();
      } catch (err) {
        toast(err.message || `Envoi de « ${file.name} » impossible.`, "err");
      }
    }
  }

  async function saveCaption(m, value) {
    if (value === (m.caption || "")) return;
    try {
      await api.updateMediaCaption(propertyId, m.id, value || null);
      m.caption = value;
      toast("Légende enregistrée.", "ok");
    } catch (err) { toast(err.message || "Enregistrement impossible.", "err"); }
  }

  async function remove(m) {
    if (!(await confirmDialog("Supprimer ce média ?", { title: "Supprimer", okLabel: "Supprimer", danger: true }))) return;
    try {
      await api.deleteMedia(propertyId, m.id);
      items = items.filter((x) => x.id !== m.id);
      render();
      toast("Média supprimé.", "ok");
    } catch (err) { toast(err.message || "Suppression impossible.", "err"); }
  }

  function move(index, dir) { reorder(index, index + dir); }

  async function reorder(from, to) {
    if (to < 0 || to >= items.length) return;
    const next = items.slice();
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    items = next;
    render();
    try {
      const updated = await api.reorderMedia(propertyId, items.map((m) => m.id));
      // Reflète l'ordre serveur pour cette section
      const bySection = updated.filter((m) => m.section_code === (sectionCode || null));
      if (bySection.length === items.length) items = bySection;
    } catch (err) {
      toast(err.message || "Réordonnancement impossible.", "err");
      load();
    }
  }
}
