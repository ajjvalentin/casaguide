/* Photo de couverture du logement (V2-30).

   Bloc mutualisable (monté dans la fiche « Informations du logement ») qui désigne
   l'image de « façade » commerciale servie HORS du guide : vignette des emails
   d'envoi (séjour/vitrine) et og-image des liens partagés (WhatsApp/iMessage).

   Deux façons de la définir (décision André : les deux) :
     · téléverser une photo dédiée (flux upload existant, SANS section → média du
       logement, qui ne figure dans aucune section du guide) ;
     · choisir une photo existante parmi les photos du guide.
   Et « Retirer » (remet la couverture à NULL — repli gracieux sur la première
   photo puis l'image générée ; ne supprime jamais le média).

   Le composant agit IMMÉDIATEMENT (PUT /cover) et remonte le logement mis à jour
   via `onChange(updatedProperty)` — indépendant du bouton « Enregistrer » de la
   fiche. Aucun recadrage/éditeur d'image (hors périmètre). */

import { api } from "../api.js";
import { el, icon, mount, toast, openModal, refreshIcons } from "../ui.js";

const ACCEPT = "image/jpeg,image/png,image/webp";

/* Vignette d'un média chargée avec le jeton (URL objet révoquée au démontage). */
function thumb(propertyId, mediaId, urls) {
  const img = el("img", { class: "cover-thumb-img", alt: "Photo de couverture" });
  api.mediaBlobUrl(propertyId, mediaId)
    .then((u) => { urls.push(u); img.src = u; })
    .catch(() => { /* média absent : le repli d'affichage suffit */ });
  return img;
}

export function buildCoverBlock(property, { onChange } = {}) {
  const pid = property.id;
  let coverId = property.cover_media_id || null;
  const objectUrls = [];

  const preview = el("div", { class: "cover-preview" });
  const fileInput = el("input", {
    type: "file", accept: ACCEPT, style: { display: "none" },
    onChange: (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) uploadCover(f); },
  });
  const uploadBtn = el("button", { class: "btn btn-sm", type: "button" },
    icon("upload", 15), "Téléverser une photo");
  const chooseBtn = el("button", { class: "btn btn-sm", type: "button" },
    icon("image", 15), "Choisir parmi les photos du guide");
  const removeBtn = el("button", { class: "btn btn-sm btn-ghost cover-remove", type: "button" },
    icon("x", 15), "Retirer");
  const busy = el("span", { class: "cover-busy hidden" }, "…");

  uploadBtn.addEventListener("click", () => fileInput.click());
  chooseBtn.addEventListener("click", openPicker);
  removeBtn.addEventListener("click", () => applyCover(null));

  // Glisser-déposer (V2-30, reliquat) : même geste que les zones photos des
  // sections (components/media.js). Un fichier lâché sur l'aperçu le téléverse
  // comme couverture (flux upload sans section, comme le bouton « Téléverser »).
  ["dragover", "dragenter"].forEach((ev) => preview.addEventListener(ev, (e) => {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault(); preview.classList.add("over");
    }
  }));
  ["dragleave", "dragend"].forEach((ev) =>
    preview.addEventListener(ev, () => preview.classList.remove("over")));
  preview.addEventListener("drop", (e) => {
    preview.classList.remove("over");
    const f = e.dataTransfer?.files?.[0];
    if (f) { e.preventDefault(); e.stopPropagation(); uploadCover(f); }
  });

  const block = el("div", { class: "cover-block field-group" },
    el("h3", { class: "form-subhead" }, "Photo de couverture"),
    el("p", { class: "help", style: { margin: "0 0 8px" } },
      "Image de vente du logement : vignette des emails et aperçu des liens partagés. "
      + "Sans couverture, la première photo du guide est utilisée."),
    preview,
    el("div", { class: "row cover-actions", style: { marginTop: "8px", gap: "6px", flexWrap: "wrap" } },
      uploadBtn, chooseBtn, removeBtn, busy),
    fileInput);

  renderPreview();
  return block;

  function setBusy(on) {
    busy.classList.toggle("hidden", !on);
    [uploadBtn, chooseBtn, removeBtn].forEach((b) => { b.disabled = on; });
  }

  function renderPreview() {
    removeBtn.classList.toggle("hidden", !coverId);
    if (coverId) {
      mount(preview, thumb(pid, coverId, objectUrls));
    } else {
      mount(preview, el("div", { class: "cover-empty muted" },
        icon("image", 20), el("span", {}, "Première photo du guide (par défaut)")));
    }
    refreshIcons();
  }

  async function applyCover(mediaId) {
    setBusy(true);
    try {
      const updated = await api.setCover(pid, mediaId);
      coverId = updated.cover_media_id || null;
      renderPreview();
      toast(mediaId ? "Photo de couverture mise à jour." : "Couverture retirée.", "ok");
      if (onChange) onChange(updated);
    } catch (e) {
      toast(e.message || "Impossible de mettre à jour la couverture.", "err");
    } finally {
      setBusy(false);
    }
  }

  async function uploadCover(file) {
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", file);          // pas de section_code → média du logement
      const media = await api.uploadMedia(pid, fd);
      await applyCover(media.id);        // applyCover gère son propre setBusy
    } catch (e) {
      toast(e.message || "Téléversement impossible.", "err");
      setBusy(false);
    }
  }

  function openPicker() {
    const grid = el("div", { class: "cover-picker" }, el("div", { class: "muted" }, "Chargement…"));
    const modal = openModal({
      title: "Choisir une photo", size: "lg", body: grid,
      footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler")],
    });
    api.listMedia(pid).then((rows) => {
      const photos = (rows || []).filter((m) => m.kind === "photo");
      if (!photos.length) {
        mount(grid, el("div", { class: "muted" },
          "Aucune photo dans ce guide pour l'instant. Ajoutez-en dans une section, "
          + "ou téléversez une photo dédiée."));
        return;
      }
      mount(grid, ...photos.map((m) => {
        const cell = el("button", { class: "cover-cell", type: "button" },
          thumb(pid, m.id, objectUrls));
        if (m.id === coverId) cell.classList.add("selected");
        cell.addEventListener("click", () => { modal.close(); applyCover(m.id); });
        return cell;
      }));
    }).catch(() => mount(grid, el("div", { class: "errbox" }, "Chargement des photos impossible.")));
  }
}
