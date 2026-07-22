/* Gestion uniforme des refus de quota (402 `quota_exceeded`, V2-05a).

   La vérité des quotas est côté serveur : les vues peuvent griser des boutons,
   mais elles DOIVENT aussi intercepter le 402 renvoyé par l'API. Ce module
   affiche un encart propre (jamais un alert() brut) invitant à changer d'offre.

   Usage dans une vue :
     catch (err) { if (!handleQuotaError(err)) toast(err.message, "err"); } */

import { ApiError } from "./api.js";
import { openModal, el } from "./ui.js";
import { navigate } from "./nav.js";

export function isQuotaError(err) {
  return err instanceof ApiError && err.detail
    && err.detail.code === "quota_exceeded";
}

/* Renvoie true si l'erreur était un dépassement de quota (et affiche l'encart),
   false sinon → l'appelant traite alors l'erreur normalement. */
export function handleQuotaError(err) {
  if (!isQuotaError(err)) return false;
  const seePlans = el("button", { class: "btn btn-primary" }, "Voir les offres");
  const close = el("button", { class: "btn btn-ghost" }, "Fermer");
  const m = openModal({
    title: "Limite de votre offre atteinte",
    body: el("div", {},
      el("p", { class: "muted", style: { margin: "0 0 6px" } }, err.message),
      el("p", { class: "muted", style: { margin: "0", fontSize: "13px" } },
        "Aucune donnée n'est perdue : passez à une offre supérieure pour "
        + "débloquer cette action.")),
    footer: [close, seePlans],
  });
  seePlans.addEventListener("click", () => { m.close(); navigate("#/abonnement"); });
  close.addEventListener("click", () => m.close());
  return true;
}
