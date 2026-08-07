/* Le fil des 7 étapes (V2-31, volet 2) — rendu partagé carte + éditeur.
 *
 * La VÉRITÉ vient du serveur (`property.journey`, calculé par `api/journey.py`) :
 * la substance, jamais la déclaration. Ce module ne fait que PEINDRE l'état
 * calculé — aucune logique de progression ici (source unique côté serveur).
 *
 * La bascule « Section complétée » a été retirée (elle nourrissait le pourcentage
 * mensonger de l'audit) : l'éditeur juge désormais une rubrique « garnie » sur son
 * CONTENU. `sectionHasSubstance` reproduit fidèlement `journey.section_has_substance`
 * du backend — duplication VOLONTAIRE (comme `js/lib/care.js`) : si l'une change,
 * changer l'autre.
 */
import { el, icon } from "./ui.js";
import { navigate } from "./nav.js";

// Codes des sections ESSENTIELLES — badge « Essentiel » dans l'éditeur. Miroir de
// `journey.ESSENTIAL_CODES` (backend) : check-in, check-out, boîte à clés, accès,
// wifi. Le reste reste sans badge (« à votre rythme »).
export const ESSENTIAL_CODES = new Set([
  "A_checkin", "A_checkout", "A_keybox", "A_access", "B_wifi"]);

/** Une section porte-t-elle un contenu réel ? (jamais un booléen décoché ni du
 * blanc). Miroir de `journey.section_has_substance` (backend). */
export function sectionHasSubstance(content, bodyMd) {
  if (bodyMd && String(bodyMd).trim()) return true;
  if (content && typeof content === "object" && !Array.isArray(content)) {
    return Object.values(content).some(valueFilled);
  }
  return false;
}

function valueFilled(v) {
  if (v == null || v === false) return false;
  if (typeof v === "string") return v.trim().length > 0;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v).length > 0;
  return true;   // nombre, booléen vrai
}

/**
 * Rend le fil pour une carte ou un en-tête : « Étape k/7 · <action> » cliquable
 * + barre de progression ; ou « Guide envoyé ✓ » au bout du chemin.
 * @param journey l'objet `property.journey` (peut être null → rien).
 * @param onNavigate callback de navigation (défaut : `navigate`).
 */
export function renderJourney(journey, { onNavigate = navigate } = {}) {
  if (!journey) return null;

  // Au bout du chemin (étape 7 faite) : la carte d'un vétéran ne fait pas la leçon.
  if (journey.sent) {
    return el("div", { class: "journey journey-sent" },
      icon("badge-check", 16), el("span", {}, "Guide envoyé"));
  }

  const a = journey.next_action;
  const pill = el("button", {
    class: "journey-fil",
    title: a ? a.label : "",
    onClick: () => { if (a) onNavigate(a.route); },
  },
    el("span", { class: "journey-step" }, `Étape ${journey.current_step}/7`),
    el("span", { class: "journey-sep" }, "·"),
    el("span", { class: "journey-next" }, a ? a.label : "Continuer"),
    icon("arrow-right", 15));

  const pct = journey.total ? Math.round((journey.done_count / journey.total) * 100) : 0;
  const bar = el("div", { class: "journey-bar" }, el("i", { style: { width: pct + "%" } }));
  return el("div", { class: "journey" }, pill, bar);
}
