/* Lien de partage élégant du guide (M-25).

   « Copier le lien » copie la forme /g/{slug}-{token} (nom lisible devant, jeton
   de sécurité intact derrière). Le slug est DÉCORATIF : côté serveur seul le
   token final fait foi, et les anciens liens nus /g/{token} restent valides.
   Le slugify reproduit celui du backend (guide_page.slugify) : ASCII, minuscules,
   tirets — l'exactitude du slug n'est pas requise (le token décide). */

export function slugify(name, maxlen = 60) {
  const ascii = (name || "")
    .normalize("NFKD").replace(/[\u0300-\u036f]/g, "");   // retire les accents
  const s = ascii.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
  return s.slice(0, maxlen).replace(/^-+|-+$/g, "") || "guide";
}

/* Lien slug du guide, éventuellement forcé dans une langue (V2-10).

   `lang` optionnel : quand il diffère de la langue par défaut du logement, on
   ajoute `?lang=xx` — la page servie est alors localisée (M-09) et sa vignette
   Open Graph (M-25) l'est aussi. Pour la langue par défaut (ou `lang` absent),
   le lien reste nu : le voyageur retombe sur la détection automatique / le fr. */
export function guideSharePath(property, lang) {
  const path = `/g/${slugify(property.name)}-${property.guide_token}`;
  const def = (property.default_lang || "fr").toLowerCase();
  const target = (lang || "").toLowerCase();
  return target && target !== def ? `${path}?lang=${target}` : path;
}

export function guideShareUrl(property, lang) {
  return location.origin + guideSharePath(property, lang);
}
