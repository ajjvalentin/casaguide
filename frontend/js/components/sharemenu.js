/* Menu « Copier le lien » multilingue (V2-10).

   Le bouton « Copier le lien » (carte du logement dans properties.js ET en-tête
   de l'éditeur dans editor.js) ouvre un petit menu FR/EN/ES — même motif que le
   menu de langue du QR (M-26, editor.openPosterMenu). Un clic copie le lien slug
   /g/{slug}-{token} avec ?lang=xx (déterministe : prime sur la détection auto du
   téléphone du voyageur), sauf pour la langue par défaut du logement (lien nu,
   cf. guideSharePath). Le premier choix du menu est la langue par défaut. */

import { el, icon, openModal, toast } from "../ui.js";
import { guideShareUrl } from "../share.js";
import { publishedLanguages } from "../languages.js";

// Langues proposées, langue par défaut du logement en tête. La liste vient du
// REGISTRE (V2-21a, langues publiées) — jamais d'une constante en dur : une
// langue dépubliée en base disparaît d'ici sans redéploiement. Renvoie une liste
// de {code, name_native}.
async function langChoices(property) {
  const def = (property.default_lang || "fr").toLowerCase();
  const published = await publishedLanguages();       // [{code, name_native}]
  const byCode = new Map(published.map((l) => [l.code, l]));
  const rest = published.filter((l) => l.code !== def);
  const defEntry = byCode.get(def) || { code: def, name_native: def.toUpperCase() };
  return [defEntry, ...rest];
}

async function copyLink(property, lang) {
  try {
    await navigator.clipboard.writeText(guideShareUrl(property, lang));
    toast(`Lien copié (${lang.toUpperCase()}).`, "ok");
  } catch (_) {
    toast("Copie impossible — copiez le lien manuellement.", "err");
  }
}

export async function openShareMenu(property) {
  const choices = await langChoices(property);
  const body = el("div", {},
    el("p", { class: "muted", style: { marginTop: 0 } },
      "Langue du lien à partager (le voyageur ouvrira le guide dans cette langue) :"),
    el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } },
      ...choices.map((l) =>
        el("button", { class: "btn", onClick: () => { menu.close(); copyLink(property, l.code); } },
          icon("link", 16), l.name_native))));
  const menu = openModal({
    title: "Copier le lien",
    body,
    footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => menu.close() }, "Fermer")],
  });
  return menu;
}
