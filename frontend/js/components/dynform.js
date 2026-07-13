/* Générateur de formulaire dynamique (M-03, §4 du CdC).

   Construit le formulaire d'une section à partir de `section_templates.field_schema` :
     - fields[]  : text, textarea, time, bool, number, select, url, phone
     - repeat    : liste de fiches ajoutables (ex. équipements, services)
     - secrets[] : champs chiffrés (wifi, boîte à clés) — envoyés à l'endpoint
                   dédié, jamais stockés dans le contenu de section côté client
     - poi_categories / area_facts / uses_property_* : encarts informatifs

   collect() renvoie { content, body_md, hasSecrets, secretsPatch } ; la
   visibilité et l'état « complété » sont gérés par le panneau de l'éditeur. */

import { el, icon, t, refreshIcons } from "../ui.js";
import { OPTION_LABELS } from "../constants.js";
import { buildWifiQrPanel } from "./wifiqr.js";

// Regroupements de champs sensibles (le field_schema ne liste que la clé
// chiffrée ; on y adjoint les champs en clair associés — SSID, notes).
const SECRET_BLOCKS = {
  wifi_pass: {
    title: "Identifiants Wifi",
    fields: [
      { key: "wifi_ssid", label: "Nom du réseau (SSID)", type: "text" },
      { key: "wifi_pass", label: "Mot de passe Wifi", type: "password" },
    ],
  },
  keybox_code: {
    title: "Boîte à clés",
    fields: [
      { key: "keybox_code", label: "Code de la boîte à clés", type: "password" },
      { key: "keybox_notes", label: "Notes (emplacement, astuces)", type: "textarea" },
    ],
  },
};

const INPUT_TYPE = { time: "time", url: "url", phone: "tel", password: "password",
                     text: "text", email: "email" };

function makeControl(field, value) {
  const type = field.type;

  if (type === "bool") {
    const input = el("input", { type: "checkbox" });
    input.checked = !!value;
    const control = el("label", { class: "switch" },
      input, el("span", { class: "track" }),
      el("span", {}, t(field.label, field.key)));
    return { control, get: () => input.checked, isSwitch: true };
  }
  if (type === "textarea") {
    const ta = el("textarea", {});
    if (value != null) ta.value = value;
    return { control: ta, get: () => ta.value.trim() || null };
  }
  if (type === "select") {
    const sel = el("select", {},
      el("option", { value: "" }, "— choisir —"),
      ...(field.options || []).map((o) => el("option", { value: o }, OPTION_LABELS[o] || o)));
    if (value != null) sel.value = value;
    return { control: sel, get: () => sel.value || null };
  }
  if (type === "number") {
    const inp = el("input", { type: "number" });
    if (value != null) inp.value = value;
    return { control: inp, get: () => (inp.value === "" ? null : Number(inp.value)) };
  }
  const inp = el("input", { type: INPUT_TYPE[type] || "text" });
  if (value != null) inp.value = value;
  return { control: inp, get: () => inp.value.trim() || null };
}

function fieldNode(field, value) {
  const { control, get, isSwitch } = makeControl(field, value);
  if (isSwitch) return { node: el("div", { class: "field" }, control), get };
  const node = el("div", { class: "field" },
    el("label", {}, t(field.label, field.key)),
    field.help ? el("div", { class: "help" }, field.help) : null,
    control);
  return { node, get };
}

function repeatGroup(repeat, values) {
  const list = el("div", {});
  const collectors = [];

  function addItem(item = {}) {
    const itemCollectors = [];
    const card = el("div", { class: "repeat-item" });
    card.append(el("button", {
      class: "btn btn-ghost btn-sm rm", type: "button", "aria-label": "Retirer",
      onClick: () => { card.remove(); const i = collectors.indexOf(entry); if (i >= 0) collectors.splice(i, 1); },
    }, icon("trash-2", 15)));
    for (const f of repeat.fields) {
      const { node, get } = fieldNode(f, item[f.key]);
      card.append(node);
      itemCollectors.push([f.key, get]);
    }
    const entry = {
      get: () => {
        const o = {};
        for (const [k, g] of itemCollectors) { const v = g(); if (v != null && v !== "") o[k] = v; }
        return o;
      },
    };
    collectors.push(entry);
    list.append(card);
    refreshIcons();
  }

  (values || []).forEach(addItem);
  const node = el("div", { class: "field" },
    list,
    el("button", { class: "btn btn-sm", type: "button", onClick: () => addItem() },
      icon("plus", 15), "Ajouter une fiche"));
  return { node, key: repeat.key, get: () => collectors.map((c) => c.get()).filter((o) => Object.keys(o).length) };
}

function aiNotice(propertyId, kind) {
  const text = kind === "poi"
    ? "Cette rubrique est alimentée par des suggestions automatiques (POI). "
    : "Les informations locales de cette rubrique (numéros d'urgence, tri, bruit…) sont pré-remplies par l'IA, à vérifier. ";
  return el("div", { class: "notice notice-ai", style: { marginBottom: "16px" } },
    icon("sparkles", 18),
    el("div", {}, text,
      kind === "poi"
        ? el("a", { href: `#/properties/${propertyId}/pois` }, "Valider les suggestions →")
        : null));
}

function propRefNotice(kind) {
  const label = kind === "contact"
    ? "Vos coordonnées de contact proviennent de la fiche du logement."
    : "Le numéro de licence touristique provient de la fiche du logement.";
  return el("div", { class: "notice notice-info", style: { marginBottom: "16px" } },
    icon("link", 18), el("div", {}, label));
}

export function buildSectionForm(section, { secrets = {}, propertyId } = {}) {
  const schema = section.field_schema || {};
  const content = section.content || {};
  const form = el("form", { class: "section-form", onSubmit: (e) => e.preventDefault() });

  // Encarts informatifs (enrichissement IA, références à la fiche logement)
  if (schema.poi_categories && schema.poi_categories.length) form.append(aiNotice(propertyId, "poi"));
  if (schema.area_facts && schema.area_facts.length) form.append(aiNotice(propertyId, "facts"));
  if (schema.uses_property_contact) form.append(propRefNotice("contact"));
  if (schema.uses_property_license) form.append(propRefNotice("license"));

  const getters = [];
  for (const f of schema.fields || []) {
    const { node, get } = fieldNode(f, content[f.key]);
    form.append(node);
    getters.push([f.key, get]);
  }

  const repeatGetters = [];
  if (schema.repeat) {
    const g = repeatGroup(schema.repeat, content[schema.repeat.key]);
    form.append(g.node);
    repeatGetters.push(g);
  }

  // Champs sensibles chiffrés
  const secretGetters = [];
  for (const sk of schema.secrets || []) {
    const block = SECRET_BLOCKS[sk];
    if (!block) continue;
    const box = el("div", { class: "secret-field" }, el("div", { class: "field-label" }, block.title));
    const controls = {};
    for (const sf of block.fields) {
      const { node, get } = fieldNode({ label: { fr: sf.label }, type: sf.type, key: sf.key }, secrets[sf.key]);
      box.append(node);
      secretGetters.push([sf.key, get]);
      controls[sf.key] = node.querySelector("input, textarea, select");
    }
    box.append(el("div", { class: "secret-note" }, icon("lock", 13),
      "Chiffré côté serveur, jamais visible par le voyageur."));
    // Aperçu du QR de connexion Wifi (M-06) — généré localement, mot de passe
    // jamais transmis ailleurs que par l'endpoint /secrets déjà utilisé.
    if (sk === "wifi_pass") {
      const qr = buildWifiQrPanel({
        getSsid: () => controls.wifi_ssid && controls.wifi_ssid.value,
        getPass: () => controls.wifi_pass && controls.wifi_pass.value,
      });
      box.append(qr.node);
      controls.wifi_ssid && controls.wifi_ssid.addEventListener("input", qr.refresh);
      controls.wifi_pass && controls.wifi_pass.addEventListener("input", qr.refresh);
    }
    form.append(box);
  }

  // Texte libre (body_md) — complément optionnel pour toute section
  const bodyTa = el("textarea", {});
  if (section.body_md) bodyTa.value = section.body_md;
  form.append(el("div", { class: "field" },
    el("label", {}, "Complément (texte libre)"),
    el("div", { class: "help" }, "Toute précision utile pour le voyageur (facultatif)."),
    bodyTa));

  function collect() {
    const c = {};
    for (const [k, g] of getters) { const v = g(); if (v != null && v !== "") c[k] = v; }
    for (const rg of repeatGetters) { const arr = rg.get(); if (arr.length) c[rg.key] = arr; }
    const secretsPatch = {};
    for (const [k, g] of secretGetters) secretsPatch[k] = g();
    return {
      content: c,
      body_md: bodyTa.value.trim() || null,
      hasSecrets: secretGetters.length > 0,
      secretsPatch,
    };
  }

  return { node: form, collect, hasSecrets: (schema.secrets || []).length > 0 };
}
