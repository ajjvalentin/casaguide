/* Gestion uniforme des refus liés au plan (V2-05a / V2-18a).

   Deux refus « métier » se traitent pareil côté front — un encart propre (jamais
   un alert() brut) invitant à changer d'offre, avec accès à #/abonnement :
     - 402 `quota_exceeded`  : limite du plan atteinte (logements, enrichissements,
       langues).
     - 403 `trial_expired`   : essai terminé → back-office en lecture seule.

   La vérité est côté serveur : les vues peuvent griser/prévenir, mais elles
   DOIVENT aussi intercepter ces refus.

   Usage dans une vue :
     catch (err) { if (!handlePlanLimit(err)) toast(err.message, "err"); } */

import { ApiError } from "./api.js";
import { openModal, el } from "./ui.js";
import { navigate } from "./nav.js";

function hasCode(err, code) {
  return err instanceof ApiError && err.detail && err.detail.code === code;
}

export function isQuotaError(err) { return hasCode(err, "quota_exceeded"); }
export function isTrialExpiredError(err) { return hasCode(err, "trial_expired"); }
export function isStaffLockedError(err) { return hasCode(err, "staff_locked"); }

/* Encart commun. Renvoie true si l'erreur relève du plan (et affiche l'encart),
   false sinon → l'appelant traite alors l'erreur normalement. Gère trois codes :
   quota_exceeded (402), trial_expired (403), staff_locked (402, cahier équipe
   réservé à l'offre Pro — V2-18b). */
export function handlePlanLimit(err) {
  const trial = isTrialExpiredError(err);
  const staff = isStaffLockedError(err);
  if (!trial && !staff && !isQuotaError(err)) return false;

  const title = trial ? "Votre essai est terminé"
    : staff ? "Réservé à l'offre Pro"
    : "Limite de votre offre atteinte";
  const note = trial
    ? "Vos guides restent en ligne. Choisissez une offre pour reprendre la main "
      + "sur vos logements — aucune donnée n'est perdue."
    : "Aucune donnée n'est perdue : passez à une offre supérieure pour débloquer "
      + "cette action.";

  const seePlans = el("button", { class: "btn btn-primary" },
    trial ? "Choisir mon offre" : staff ? "Passer à l'offre Pro" : "Voir les offres");
  const close = el("button", { class: "btn btn-ghost" }, "Fermer");
  const m = openModal({
    title,
    body: el("div", {},
      el("p", { class: "muted", style: { margin: "0 0 6px" } }, err.message),
      el("p", { class: "muted", style: { margin: "0", fontSize: "13px" } }, note)),
    footer: [close, seePlans],
  });
  seePlans.addEventListener("click", () => { m.close(); navigate("#/abonnement"); });
  close.addEventListener("click", () => m.close());
  return true;
}

/* Rétrocompat : les vues existantes importent `handleQuotaError` — il gère
   désormais aussi le refus d'essai expiré (`trial_expired`). */
export const handleQuotaError = handlePlanLimit;
