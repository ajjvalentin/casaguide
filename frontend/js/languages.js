/* Registre des langues côté back-office (V2-21a).

   Source UNIQUE des langues offertes par le produit : l'endpoint `GET /languages`
   (langues `published` du registre). Plus aucune liste de langues en dur dans le
   front — une langue passée en brouillon en base disparaît de tous les sélecteurs
   (partage, modale séjour) sans redéploiement.

   Le registre est stable pendant une session : on le charge une fois et on met la
   promesse en cache. En cas d'échec réseau, on renvoie un repli minimal (les trois
   langues historiques) pour ne jamais casser un menu — le serveur reste l'autorité. */

import { api } from "./api.js";

let _cache = null;   // Promise<Array<{code, name_native}>>

// Repli si l'endpoint est injoignable : l'état actuel du produit (fr/en/es).
// N'est utilisé qu'en cas d'erreur réseau — le registre en base fait foi.
const FALLBACK = [
  { code: "fr", name_native: "Français" },
  { code: "en", name_native: "English" },
  { code: "es", name_native: "Español" },
];

/* Langues publiées, ordonnées comme le registre : [{code, name_native}]. */
export function publishedLanguages() {
  if (!_cache) {
    _cache = api.listLanguages()
      .then((rows) => (Array.isArray(rows) && rows.length ? rows : FALLBACK))
      .catch(() => FALLBACK);
  }
  return _cache;
}

/* Ensemble des codes publiés (pour filtrer une liste de langues d'un logement). */
export async function publishedLanguageCodes() {
  return (await publishedLanguages()).map((l) => l.code);
}

/* Nom natif d'une langue si elle est publiée, sinon son code brut. */
export async function languageName(code) {
  const found = (await publishedLanguages()).find((l) => l.code === code);
  return found ? found.name_native : code;
}
