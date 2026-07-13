/* Aperçu du QR de connexion Wifi côté back-office (M-06, §3.2).

   Réutilise le module QR mutualisé (frontend/guide/qr.js) : aucune duplication
   de l'algorithme. Le QR est généré **localement dans le navigateur** à partir
   des identifiants déjà présents dans l'éditeur (chargés via GET /secrets,
   réservé au propriétaire) — le mot de passe ne transite par AUCUN autre canal :
   ni requête réseau supplémentaire, ni téléchargement serveur. Le bouton produit
   un PNG haute résolution (canvas.toDataURL) pour impression séparée. */

import { qrCanvas, wifiPayload } from "../../guide/qr.js";
import { el, icon, mount } from "../ui.js";

/* getSsid()/getPass() lisent en direct les champs de l'éditeur (mise à jour
   live du QR). Retourne { node, refresh } ; l'appelant câble refresh() sur les
   événements `input` des deux champs. */
export function buildWifiQrPanel({ getSsid, getPass }) {
  const holder = el("div", { class: "wifiqr-canvas" });
  const hint = el("div", { class: "help", style: { margin: "0" } });
  const dlBtn = el("button", { class: "btn btn-sm", type: "button" },
    icon("download", 15), "Télécharger le QR (PNG)");

  function values() {
    return { ssid: (getSsid() || "").trim(), pass: (getPass() || "").trim() };
  }

  function refresh() {
    const { ssid, pass } = values();
    if (!ssid || !pass) {
      mount(holder, el("div", { class: "wifiqr-empty" },
        "Renseignez le nom du réseau et le mot de passe pour générer le QR."));
      hint.textContent = "";
      dlBtn.disabled = true;
      return;
    }
    const canvas = qrCanvas(wifiPayload(ssid, pass),
      { scale: 6, label: "QR de connexion Wifi" });
    if (!canvas) {
      mount(holder, el("div", { class: "wifiqr-empty" },
        "Identifiants trop longs pour un QR."));
      dlBtn.disabled = true;
      return;
    }
    mount(holder, canvas);
    hint.textContent = "Les voyageurs scannent ce code pour se connecter automatiquement.";
    dlBtn.disabled = false;
  }

  dlBtn.addEventListener("click", () => {
    const { ssid, pass } = values();
    if (!ssid || !pass) return;
    // PNG haute résolution dédié à l'impression (indépendant de l'aperçu)
    const big = qrCanvas(wifiPayload(ssid, pass), { scale: 16 });
    if (!big) return;
    const a = el("a", { href: big.toDataURL("image/png"), download: "wifi-qr.png" });
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  const node = el("div", { class: "wifiqr" },
    el("div", { class: "field-label" }, icon("qr-code", 15), " QR de connexion Wifi"),
    el("div", { class: "help" },
      "Aperçu du QR à imprimer et laisser dans le logement (feuille séparée)."),
    holder,
    el("div", { class: "wifiqr-foot" }, hint, dlBtn));

  refresh();
  return { node, refresh };
}
