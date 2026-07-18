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

export function guideSharePath(property) {
  return `/g/${slugify(property.name)}-${property.guide_token}`;
}

export function guideShareUrl(property) {
  return location.origin + guideSharePath(property);
}
