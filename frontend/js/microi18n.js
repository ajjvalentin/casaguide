/* Micro-i18n des surfaces PUBLIQUES (tunnel voyageur, vitrine) — V2-58.

   Le back-office est FR seul ; les surfaces grand public sont FR/EN/ES (structure
   prête pour les 7). Pas de catalogue global : chaque vue porte son dictionnaire
   { fr:{}, en:{}, es:{} }, ce module ne fournit que la résolution de langue et le
   substituteur. Même convention `?lang=` + `localStorage("casaguide:lang")` que le
   guide (M-09). */

export const PUBLIC_LANGS = ["fr", "en", "es"];

export function pickLang(params, allowed = PUBLIC_LANGS) {
  const raw = (params && params.get && params.get("lang"))
    || localStorage.getItem("casaguide:lang")
    || (navigator.language || "fr").slice(0, 2);
  const c = String(raw).toLowerCase();
  return allowed.includes(c) ? c : allowed[0];
}

export function setLang(lang) {
  if (PUBLIC_LANGS.includes(lang)) localStorage.setItem("casaguide:lang", lang);
}

/* Renvoie un traducteur `tr(key, subs?)` : STRINGS[lang][key] → repli fr → clé.
   `subs` remplace les {placeholders}. */
export function translator(STRINGS, lang) {
  return (key, subs) => {
    let s = (STRINGS[lang] && STRINGS[lang][key]) || STRINGS.fr[key] || key;
    if (subs) for (const k in subs) s = s.replaceAll(`{${k}}`, subs[k]);
    return s;
  };
}
