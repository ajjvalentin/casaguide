/* Fiche du logement éditable (M-24).

   Deux modales mutualisées entre « Mes logements » (carte) et l'éditeur :

     · openPropertyInfoModal(property, { onSaved }) — « Informations du logement »
       (nom, adresse complète, contact voyageur, licence). Le PATCH existe depuis
       F-06 mais aucune interface ne l'appelait. À la modification d'adresse, on
       PROPOSE (sans imposer) un re-géocodage + recalcul des distances : la case
       est décochée par défaut quand la position a été placée à la main
       (geocode_source='manual'), pour ne JAMAIS écraser un point manuel sans
       accord explicite.

     · openPositionModal(property, { onSaved }) — mini-carte de placement du point
       (M-05), désormais accessible À TOUT MOMENT (plus seulement quand le
       géocodage n'est pas « rooftop ») : une position « précise » peut être
       fausse (vécu le 16/07).

   Les deux renvoient le logement mis à jour via `onSaved(updatedProperty)` pour
   que l'appelant rafraîchisse son affichage. */

import { api } from "../api.js";
import { el, icon, toast, openModal, confirmDialog, refreshIcons } from "../ui.js";
import { COUNTRIES } from "../constants.js";

// Champs d'adresse : leur modification rend le re-géocodage pertinent.
const ADDRESS_KEYS = ["address_line1", "address_line2", "postal_code", "city", "country_code"];

function field(label, name, value, opts = {}) {
  const input = el("input", { type: opts.type || "text", name, value: value ?? "", ...(opts.attrs || {}) });
  return { input, node: el("div", { class: "field" }, el("label", {}, label), input) };
}

export function openPropertyInfoModal(property, { onSaved } = {}) {
  const name = field("Nom du logement", "name", property.name, { attrs: { required: true } });
  const addr1 = field("Adresse", "address_line1", property.address_line1, { attrs: { required: true } });
  const addr2 = field("Complément d'adresse", "address_line2", property.address_line2);
  const postal = field("Code postal", "postal_code", property.postal_code);
  const city = field("Ville", "city", property.city, { attrs: { required: true } });
  const region = field("Région", "region", property.region);

  const countrySel = el("select", { name: "country_code", required: true },
    ...COUNTRIES.map(([code, nm]) => el("option", { value: code }, `${nm} (${code})`)));
  countrySel.value = property.country_code || "ES";
  const country = el("div", { class: "field" }, el("label", {}, "Pays"), countrySel);

  const cName = field("Nom du contact", "contact_name", property.contact_name);
  const cPhone = field("Téléphone", "contact_phone", property.contact_phone, { attrs: { type: "tel" } });
  const cWa = field("WhatsApp", "contact_whatsapp", property.contact_whatsapp, { attrs: { type: "tel" } });
  const cEmail = field("Email", "contact_email", property.contact_email, { attrs: { type: "email" } });
  const cBackup = field("Contact de secours", "contact_backup", property.contact_backup);
  const license = field("Licence touristique", "tourism_license", property.tourism_license);

  // Re-géocodage : décoché par défaut si la position est manuelle (ne jamais
  // l'écraser sans accord explicite). Pertinent seulement si l'adresse change.
  const isManual = property.geocode_source === "manual";
  const regeo = el("input", { type: "checkbox" });
  regeo.checked = !isManual;
  const regeoRow = el("label", { class: "switch", style: { marginTop: "6px" } },
    regeo, el("span", { class: "track" }),
    el("span", {}, "Re-localiser l'adresse et recalculer les distances"));
  const regeoHint = el("div", { class: "help", style: { margin: "2px 0 0" } });
  const syncRegeoHint = () => {
    const changed = ADDRESS_KEYS.some((k) => {
      const cur = (property[k] ?? "").toString().trim();
      const now = (getVal(k) ?? "").toString().trim();
      return cur !== now;
    });
    regeoRow.style.opacity = changed ? "1" : ".55";
    regeoHint.textContent = changed
      ? (isManual
        ? "Votre point a été placé à la main : cochez pour repartir de l'adresse (il sera remplacé)."
        : "L'adresse a changé : le point sera recalculé depuis la nouvelle adresse.")
      : "S'applique uniquement si vous modifiez l'adresse ci-dessus.";
  };
  function getVal(k) {
    return { address_line1: addr1.input, address_line2: addr2.input, postal_code: postal.input,
      city: city.input, country_code: countrySel }[k].value;
  }
  [addr1, addr2, postal, city].forEach((f) => f.input.addEventListener("input", syncRegeoHint));
  countrySel.addEventListener("change", syncRegeoHint);

  const posBtn = el("button", { class: "btn btn-sm", type: "button" },
    icon("map-pin", 15), "Ajuster la position sur la carte");
  posBtn.addEventListener("click", () => {
    modal.close();
    openPositionModal(property, { onSaved });
  });

  const err = el("div", { class: "errbox hidden" });
  const save = el("button", { class: "btn btn-primary" }, "Enregistrer");

  const form = el("form", { onSubmit: onSubmit },
    name.node, addr1.node, addr2.node,
    el("div", { class: "grid-2" }, postal.node, city.node),
    el("div", { class: "grid-2" }, region.node, country),
    el("div", { class: "field-group" }, regeoRow, regeoHint),
    el("div", { class: "row", style: { marginTop: "6px" } }, posBtn),
    el("h3", { class: "form-subhead" }, "Contact voyageur"),
    cName.node,
    el("div", { class: "grid-2" }, cPhone.node, cWa.node),
    el("div", { class: "grid-2" }, cEmail.node, cBackup.node),
    el("h3", { class: "form-subhead" }, "Autorisation"),
    license.node,
    err);

  const modal = openModal({
    title: "Informations du logement", size: "lg", body: form,
    footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler"), save],
  });
  save.addEventListener("click", () => form.requestSubmit());
  syncRegeoHint();
  refreshIcons();

  async function onSubmit(e) {
    e.preventDefault();
    err.classList.add("hidden");
    const patch = {
      name: name.input.value.trim(),
      address_line1: addr1.input.value.trim(),
      address_line2: addr2.input.value.trim() || null,
      postal_code: postal.input.value.trim() || null,
      city: city.input.value.trim(),
      region: region.input.value.trim() || null,
      country_code: countrySel.value,
      contact_name: cName.input.value.trim() || null,
      contact_phone: cPhone.input.value.trim() || null,
      contact_whatsapp: cWa.input.value.trim() || null,
      contact_email: cEmail.input.value.trim() || null,
      contact_backup: cBackup.input.value.trim() || null,
      tourism_license: license.input.value.trim() || null,
    };
    if (!patch.name || !patch.address_line1 || !patch.city) {
      err.textContent = "Nom, adresse et ville sont obligatoires."; err.classList.remove("hidden"); return;
    }
    const addressChanged = ADDRESS_KEYS.some((k) =>
      (property[k] ?? "").toString().trim() !== (patch[k] ?? "").toString().trim());

    save.disabled = true; save.textContent = "Enregistrement…";
    try {
      let updated = await api.updateProperty(property.id, patch);
      // Re-géocodage seulement si l'adresse a changé ET que le propriétaire l'a
      // demandé (jamais automatique — invariant M-24).
      if (addressChanged && regeo.checked) {
        try {
          const res = await api.geocodeProperty(property.id);
          updated = res.property;
          toast(`Adresse re-localisée (${res.distances_updated} lieu(x) recalculé(s)).`, "ok");
        } catch (ge) {
          toast(ge.message || "Re-localisation impossible : ajustez le point à la main.", "err");
        }
      } else {
        toast("Informations enregistrées.", "ok");
      }
      modal.close();
      if (onSaved) onSaved(updated);
    } catch (e2) {
      err.textContent = e2.message || "Enregistrement impossible."; err.classList.remove("hidden");
      save.disabled = false; save.textContent = "Enregistrer";
    }
  }
}

export function openPositionModal(property, { onSaved } = {}) {
  const mapEl = el("div", { id: "pos-map" });
  const coordLine = el("div", { class: "muted", style: { fontSize: "12.5px", marginTop: "8px" } });
  const saveBtn = el("button", { class: "btn btn-primary" }, "Enregistrer la position");
  const modal = openModal({
    title: "Position du logement", size: "lg",
    body: el("div", {},
      el("p", { class: "muted", style: { marginTop: 0 } },
        "Faites glisser le marqueur (ou cliquez sur la carte) pour placer précisément l'entrée du logement. "
        + "Vous pouvez l'ajuster à tout moment, même si l'adresse semble déjà bien localisée."),
      mapEl, coordLine),
    footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => modal.close() }, "Annuler"), saveBtn],
  });

  // Position de départ : la position existante, sinon un repli large (Espagne).
  let lat = property.lat ?? 40.0, lon = property.lon ?? -3.7;
  const zoom = property.lat == null ? 5 : 16;
  const map = L.map(mapEl).setView([lat, lon], zoom);
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
      const updated = await api.updateProperty(property.id, { lat, lon });
      modal.close();
      toast("Position mise à jour.", "ok");
      if (onSaved) onSaved(updated);
      if (await confirmDialog("Recalculer les distances de tous les lieux suggérés depuis la nouvelle position ?",
        { title: "Recalculer les distances", okLabel: "Recalculer" })) {
        try {
          const r = await api.recomputeDistances(property.id);
          toast(`Distances recalculées pour ${r.updated} lieu(x).`, "ok");
        } catch (e2) { toast(e2.message || "Recalcul impossible.", "err"); }
      }
    } catch (err) {
      toast(err.message || "Enregistrement impossible.", "err");
      saveBtn.disabled = false; saveBtn.textContent = "Enregistrer la position";
    }
  });
}
