/* Menu « Copier le lien » multilingue (V2-10).

   Le bouton « Copier le lien » (carte du logement dans properties.js ET en-tête
   de l'éditeur dans editor.js) ouvre un petit menu FR/EN/ES — même motif que le
   menu de langue du QR (M-26, editor.openPosterMenu). Un clic copie le lien slug
   /g/{slug}-{token} avec ?lang=xx (déterministe : prime sur la détection auto du
   téléphone du voyageur), sauf pour la langue par défaut du logement (lien nu,
   cf. guideSharePath). Le premier choix du menu est la langue par défaut. */

import { el, icon, openModal, toast } from "../ui.js";
import { guideShareUrl } from "../share.js";

const LANG_LABELS = { fr: "Français", en: "English", es: "Español" };
const LANG_ORDER = ["fr", "en", "es"];

// Langues proposées, langue par défaut du logement en tête.
function langChoices(property) {
  const def = (property.default_lang || "fr").toLowerCase();
  const rest = LANG_ORDER.filter((l) => l !== def);
  return [def, ...rest].filter((l) => LANG_LABELS[l]);
}

async function copyLink(property, lang) {
  try {
    await navigator.clipboard.writeText(guideShareUrl(property, lang));
    toast(`Lien copié (${lang.toUpperCase()}).`, "ok");
  } catch (_) {
    toast("Copie impossible — copiez le lien manuellement.", "err");
  }
}

export function openShareMenu(property) {
  const body = el("div", {},
    el("p", { class: "muted", style: { marginTop: 0 } },
      "Langue du lien à partager (le voyageur ouvrira le guide dans cette langue) :"),
    el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } },
      ...langChoices(property).map((code) =>
        el("button", { class: "btn", onClick: () => { menu.close(); copyLink(property, code); } },
          icon("link", 16), LANG_LABELS[code]))));
  const menu = openModal({
    title: "Copier le lien",
    body,
    footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => menu.close() }, "Fermer")],
  });
  return menu;
}
