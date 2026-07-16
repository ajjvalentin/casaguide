/* Éditeur de réseaux wifi multiples côté back-office (M-15, §8).

   Liste répétable de réseaux { label (nom d'usage : Maison, Terrasse, Étage…),
   ssid, pass }. Chaque réseau a son aperçu de QR de connexion (norme WIFI:…) et
   son bouton de téléchargement PNG. Le QR est généré LOCALEMENT dans le navigateur
   (module mutualisé guide/qr.js) à partir des identifiants déjà chargés via
   GET /secrets — le mot de passe ne transite par aucun autre canal (invariant 5).

   collect() renvoie la liste des réseaux [{label, ssid, pass}] (entrées vides
   écartées), envoyée à PUT /secrets sous la clé `wifi_networks`. */

import { qrCanvas, wifiPayload } from "../../guide/qr.js";
import { el, icon, mount, refreshIcons } from "../ui.js";

export function buildWifiNetworksPanel({ networks = [] } = {}) {
  const list = el("div", { class: "wifi-nets" });
  const cards = [];

  function addNetwork(net = {}) {
    const label = el("input", { type: "text", placeholder: "Maison, Terrasse, Étage…" });
    const ssid = el("input", { type: "text", placeholder: "Nom du réseau (SSID)" });
    const pass = el("input", { type: "text", placeholder: "Mot de passe" });
    if (net.label != null) label.value = net.label;
    if (net.ssid != null) ssid.value = net.ssid;
    if (net.pass != null) pass.value = net.pass;

    const qrHolder = el("div", { class: "wifiqr-canvas" });
    const dlBtn = el("button", { class: "btn btn-sm", type: "button" },
      icon("download", 15), "QR (PNG)");

    function refresh() {
      const s = ssid.value.trim(), p = pass.value.trim();
      if (!s || !p) {
        mount(qrHolder, el("div", { class: "wifiqr-empty" },
          "SSID + mot de passe pour générer le QR."));
        dlBtn.disabled = true;
        return;
      }
      const canvas = qrCanvas(wifiPayload(s, p), { scale: 5, label: "QR de connexion Wifi" });
      if (!canvas) {
        mount(qrHolder, el("div", { class: "wifiqr-empty" }, "Identifiants trop longs pour un QR."));
        dlBtn.disabled = true;
        return;
      }
      mount(qrHolder, canvas);
      dlBtn.disabled = false;
    }

    dlBtn.addEventListener("click", () => {
      const s = ssid.value.trim(), p = pass.value.trim();
      if (!s || !p) return;
      const big = qrCanvas(wifiPayload(s, p), { scale: 16 });
      if (!big) return;
      const fname = "wifi-" + (label.value.trim() || s).toLowerCase().replace(/[^a-z0-9]+/g, "-") + ".png";
      const a = el("a", { href: big.toDataURL("image/png"), download: fname });
      document.body.appendChild(a); a.click(); a.remove();
    });

    ssid.addEventListener("input", refresh);
    pass.addEventListener("input", refresh);

    const card = el("div", { class: "wifi-net" });
    const rm = el("button", {
      class: "btn btn-ghost btn-sm rm", type: "button", "aria-label": "Retirer ce réseau",
      onClick: () => { card.remove(); const i = cards.indexOf(entry); if (i >= 0) cards.splice(i, 1); },
    }, icon("trash-2", 15));
    card.append(rm,
      field("Nom d'usage", label),
      field("Réseau (SSID)", ssid),
      field("Mot de passe", pass),
      el("div", { class: "wifi-net-qr" }, qrHolder, dlBtn));

    const entry = {
      get: () => ({
        label: label.value.trim() || null,
        ssid: ssid.value.trim() || null,
        pass: pass.value.trim() || null,
      }),
    };
    cards.push(entry);
    list.append(card);
    refresh();
    refreshIcons();
  }

  (networks.length ? networks : [{}]).forEach(addNetwork);

  const node = el("div", { class: "secret-field" },
    el("div", { class: "field-label" }, icon("wifi", 15), " Réseaux Wifi"),
    el("div", { class: "help" },
      "Ajoutez un réseau par zone (intérieur, terrasse, étage…). Chacun a son QR à imprimer."),
    list,
    el("button", { class: "btn btn-sm", type: "button", onClick: () => addNetwork() },
      icon("plus", 15), "Ajouter un réseau"),
    el("div", { class: "secret-note" }, icon("lock", 13),
      "Chiffré côté serveur, jamais visible par le voyageur."));

  function collect() {
    return cards.map((c) => c.get()).filter((n) => n.ssid || n.pass);
  }

  return { node, collect };
}

function field(labelText, control) {
  return el("div", { class: "field" }, el("label", {}, labelText), control);
}
