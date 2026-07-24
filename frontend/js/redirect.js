/* Redirection du navigateur vers une URL externe (Checkout / portail Stripe,
   V2-05b). Centralisé ici pour un point unique de sortie hors application —
   remplaçable dans les tests headless (via import map) sans toucher aux vues. */
export function redirect(url) {
  window.location.assign(url);
}
