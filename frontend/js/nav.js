/* Navigation par ancre. navigate() force un rendu même si l'ancre est identique
   (utile après une action qui doit rafraîchir la même vue). */
export function navigate(hash) {
  if (location.hash === hash) window.dispatchEvent(new HashChangeEvent("hashchange"));
  else location.hash = hash;
}
