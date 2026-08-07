/* « Premiers pas » (V2-31, volet 3b) — le mode d'emploi interactif du back-office.
 *
 * La marche à suivre LINÉAIRE que le repli de la recherche d'aide (volet 3a)
 * promettait : la CHAÎNE cible en 7 étapes (docs/audit_ux.md §2), racontée dans le
 * ton pesé de l'audit. Chaque étape porte son étiquette (« Vous fournissez » /
 * « Holaguia s'en charge » / …), un bouton d'action (deep-link résolu contre le
 * logement courant, repli « Mes logements » — patron du volet 3a), et, dépliables,
 * les rubriques d'aide qui la concernent (rangées par le champ `step` de l'index).
 *
 * Accessible sans logement : un compte vierge doit pouvoir la lire (route
 * #/premiers-pas). Back-office FR délibéré → hors inventaire i18n voyageur.
 */

import { api } from "../api.js";
import { el, icon, mount, refreshIcons, loadingBlock } from "../ui.js";
import { navigate } from "../nav.js";
import { HELP_INDEX } from "../help/index.js";
import { resolveRoute } from "../help/search.js";

// Le texte est PESÉ (audit §2) — verbatim, jamais réécrit. `kind` colore
// l'étiquette (fourni / auto / rythme / décision). `cta.route` est un gabarit de
// route existante (`:id` = logement courant) ; `cta.label` peut dépendre de la
// présence d'un logement (étape 1 : créer vs ouvrir la fiche).
export const PREMIERS_PAS_STEPS = [
  {
    n: 1, title: "Votre logement", tag: "Vous fournissez", kind: "provide",
    body: "Donnez-lui son nom et son adresse exacte : c'est elle qui pilote tout le "
      + "reste — la carte, les distances, les lieux suggérés. Ajoutez le contact que "
      + "verront vos voyageurs, et une photo de couverture : c'est l'image de vente de "
      + "votre logement, dans les emails et les aperçus WhatsApp.",
    cta: {
      route: "#/properties/:id/editor",
      label: (hasProp) => (hasProp ? "Ouvrir la fiche Informations" : "Créer mon logement"),
    },
  },
  {
    n: 2, title: "Les indispensables du séjour", tag: "Vous fournissez", kind: "provide",
    body: "Arrivée, départ, accès au logement, boîte à clés et wifi. Sans eux, un "
      + "guide ne sert à rien à votre voyageur. Les codes se saisissent dans l'espace "
      + "sécurisé : ils sont chiffrés, et ne se montrent qu'à qui doit les voir.",
    cta: { route: "#/properties/:id/editor", label: () => "Ouvrir l'éditeur du guide" },
  },
  {
    n: 3, title: "Holaguia cherche les lieux", tag: "Holaguia s'en charge", kind: "auto",
    body: "À partir de votre adresse, Holaguia repère commerces, plages, restaurants, "
      + "pharmacies, urgences — avec les distances depuis chez vous. Lancez la recherche, "
      + "puis laissez faire : vous validerez ensuite.",
    cta: { route: "#/properties", label: () => "Rechercher les lieux" },
  },
  {
    n: 4, title: "Vous validez et personnalisez", tag: "Vous fournissez", kind: "provide",
    body: "Parcourez les lieux suggérés : gardez, écartez, corrigez. Ajoutez vos coups "
      + "de cœur — le restaurant que vous recommandez de vive voix a sa place ici. C'est "
      + "votre regard qui transforme une liste en guide.",
    cta: { route: "#/properties/:id/pois", label: () => "Voir les lieux suggérés" },
  },
  {
    n: 5, title: "Le reste du guide", tag: "À votre rythme", kind: "rhythm",
    body: "Équipements, règlement, bonnes adresses, transports… Complétez ce qui compte "
      + "pour vous, masquez ce qui ne s'applique pas. Une rubrique vide n'apparaît jamais "
      + "chez le voyageur : le guide est toujours propre.",
    cta: { route: "#/properties/:id/editor", label: () => "Ouvrir l'éditeur" },
  },
  {
    n: 6, title: "Publiez et testez", tag: "Vous décidez", kind: "decide",
    body: "Publiez, puis ouvrez le guide comme le verra votre voyageur. Imprimez le QR "
      + "pour la maison. Vous restez maître : dépubliez, corrigez, republiez à volonté.",
    cta: { route: "#/properties/:id/editor", label: () => "Voir le guide" },
  },
  {
    n: 7, title: "Envoyez", tag: "Holaguia s'en charge — vous gardez la main", kind: "auto",
    body: "Envoyez le guide en un clic : email aux couleurs de Holaguia, WhatsApp depuis "
      + "votre téléphone, lien ou QR. Et sept jours avant chaque arrivée, si la fiche du "
      + "séjour a un email, le guide part tout seul.",
    cta: { route: "#/properties", label: () => "Envoyer le guide" },
  },
];

// Le texte d'accueil (verbatim, audit §2) — en tête de page ET, en deux phrases,
// dans l'état vide de « Mes logements » (branchement §3). Exporté pour ce dernier.
export const WELCOME_INTRO =
  "Bienvenue. Un guide Holaguia se construit en sept étapes. Les deux premières vous "
  + "demandent ce que vous seul savez ; ensuite, Holaguia travaille pour vous. Vous "
  + "pouvez interrompre, revenir, corriger — rien n'est jamais bloqué.";
export const WELCOME_SHORT =
  "Un guide Holaguia se construit en sept étapes. Les deux premières vous demandent ce "
  + "que vous seul savez ; ensuite, Holaguia travaille pour vous.";

/** Résout un gabarit de route contre un logement « courant » choisi (repli sinon).
 * Réutilise `resolveRoute` (patron du volet 3a) via un hash synthétique — la page
 * « Premiers pas » n'a pas d'`:id` dans SON propre hash, on lui en fournit un. */
function makeResolver(currentId) {
  const hash = currentId ? `#/properties/${currentId}/x` : "";
  return (route) => resolveRoute(route, hash);
}

// Une rubrique d'aide : question cliquable → étapes + « M'y emmener » (même rendu
// que le panneau, classes `.help-*` partagées). Repliée par défaut, dépliable.
function helpRubric(entry, resolve) {
  const body = el("div", { class: "pp-rubric-body hidden" },
    el("ol", { class: "help-steps" }, ...entry.steps.map((s) => el("li", {}, s))),
    (() => {
      const go = el("button", { class: "btn btn-sm btn-primary help-go" },
        icon("arrow-right", 15), entry.target.label || "M'y emmener");
      go.addEventListener("click", () => navigate(resolve(entry.target.route)));
      return el("div", { class: "help-card-foot" }, go);
    })());
  const q = el("button", { class: "pp-rubric-q", "aria-expanded": "false" },
    icon("circle-help", 15), el("span", {}, entry.question), icon("chevron-down", 15));
  q.addEventListener("click", () => {
    const open = body.classList.toggle("hidden") === false;
    q.setAttribute("aria-expanded", open ? "true" : "false");
    q.classList.toggle("open", open);
  });
  return el("div", { class: "pp-rubric", dataset: { id: entry.id } }, q, body);
}

function stepCard(step, resolve, hasProp) {
  const rubrics = HELP_INDEX.filter((e) => e.step === step.n);
  const label = step.cta.label(hasProp);
  const cta = el("button", { class: "btn btn-primary pp-cta" },
    el("span", {}, label), icon("arrow-right", 16));
  cta.addEventListener("click", () => navigate(resolve(step.cta.route)));

  return el("div", { class: "pp-step", dataset: { step: String(step.n) } },
    el("div", { class: "pp-num" }, String(step.n)),
    el("div", { class: "pp-body" },
      el("div", { class: "pp-head" },
        el("h2", { class: "pp-title" }, step.title),
        el("span", { class: "pp-tag pp-tag-" + step.kind }, step.tag)),
      el("p", { class: "pp-text" }, step.body),
      el("div", { class: "pp-actions" }, cta),
      rubrics.length
        ? el("div", { class: "pp-rubrics" }, ...rubrics.map((e) => helpRubric(e, resolve)))
        : null));
}

// Bloc final « Et aussi » : les rubriques hors chemin heureux (step === null).
function alsoBlock(resolve) {
  const rubrics = HELP_INDEX.filter((e) => e.step == null);
  if (!rubrics.length) return null;
  return el("div", { class: "pp-also" },
    el("h2", { class: "pp-also-title" }, icon("compass", 18), "Et aussi"),
    el("p", { class: "pp-also-sub muted" },
      "Une fois le guide lancé : calendrier des séjours, abonnement, équipe d'entretien…"),
    el("div", { class: "pp-rubrics" }, ...rubrics.map((e) => helpRubric(e, resolve))));
}

export async function renderPremiersPas(view) {
  mount(view, el("div", { class: "page" }, loadingBlock("Chargement…")));

  // Logement « courant » = le premier du compte, s'il y en a un (pour résoudre les
  // deep-links `:id`). Best-effort : la page reste lisible sans aucun logement.
  let currentId = null;
  try {
    const props = await api.listProperties();
    if (Array.isArray(props) && props.length) currentId = props[0].id;
  } catch (_) { /* compte vierge / hors-ligne : repli « Mes logements » */ }

  const resolve = makeResolver(currentId);
  const hasProp = !!currentId;

  const page = el("div", { class: "page premiers-pas" },
    el("div", {},
      el("div", { class: "eyebrow" }, "Espace propriétaire"),
      el("h1", { class: "page-title", style: { margin: "2px 0 0" } }, "Premiers pas")),
    el("p", { class: "pp-intro" }, WELCOME_INTRO),
    el("div", { class: "pp-steps" }, ...PREMIERS_PAS_STEPS.map((s) => stepCard(s, resolve, hasProp))),
    alsoBlock(resolve));

  mount(view, page);
  refreshIcons();
}
