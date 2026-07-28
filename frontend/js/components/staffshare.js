/* Outils de transmission du cahier de l'équipe d'entretien (V2-26).

   Rattachés à la porte « Équipe d'entretien » de la carte du logement (et repris
   dans l'éditeur, contexte staff) : lien /s/{staff_token} copiable + QR du même
   lien, généré LOCALEMENT dans le navigateur (module mutualisé guide/qr.js, comme
   le QR wifi M-06) et téléchargeable en PNG. Ce n'est PAS l'affiche PDF complète
   (poster M-26, réservée au guide voyageur) : un QR simple suffit à transmettre le
   lien à l'équipe. Le QR encode l'URL publique du cahier — jamais un secret. */

import { el, icon, openModal, toast } from "../ui.js";
import { qrCanvas } from "../../guide/qr.js";

export function staffUrl(property) {
  return location.origin + `/s/${property.staff_token}`;
}

async function copyStaffLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast("Lien du cahier copié.", "ok");
  } catch (_) {
    toast("Copie impossible — copiez le lien manuellement.", "err");
  }
}

function downloadQr(canvas, property) {
  try {
    const a = el("a", { href: canvas.toDataURL("image/png"),
      download: `holaguia-equipe-${property.name || property.staff_token}.png` });
    document.body.appendChild(a); a.click(); a.remove();
    toast("QR de l'équipe téléchargé.", "ok");
  } catch (_) { toast("Téléchargement impossible.", "err"); }
}

export function openStaffShareMenu(property) {
  const url = staffUrl(property);
  const input = el("input", { type: "text", value: url, readonly: true,
    onFocus: (e) => e.target.select() });
  const copy = el("button", { class: "btn btn-sm", type: "button",
    onClick: () => copyStaffLink(url) }, icon("link", 15), "Copier");
  const open = el("a", { class: "btn btn-sm", href: `/s/${property.staff_token}`,
    target: "_blank", rel: "noopener" }, icon("external-link", 15), "Ouvrir");

  // QR du lien (généré côté navigateur ; encode l'URL publique du cahier).
  const canvas = qrCanvas(url, { scale: 6, label: "QR du cahier de l'équipe d'entretien" });
  const qrBlock = canvas
    ? el("div", { class: "qr-share" }, canvas,
        el("button", { class: "btn btn-sm", type: "button",
          onClick: () => downloadQr(canvas, property) },
          icon("download", 15), "Télécharger le QR (PNG)"))
    : null;

  const body = el("div", {},
    el("p", { class: "muted", style: { marginTop: 0 } },
      "À partager avec votre équipe d'entretien uniquement. Ce lien ne contient "
      + "jamais le wifi, la boîte à clés, ni la carte des lieux, et reste accessible "
      + "même avant la publication du guide."),
    el("div", { class: "field" }, el("label", {}, "Lien du cahier"),
      el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } }, input, copy, open)),
    qrBlock);
  const menu = openModal({
    title: "Transmettre le cahier d'équipe", body,
    footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => menu.close() }, "Fermer")],
  });
  return menu;
}
