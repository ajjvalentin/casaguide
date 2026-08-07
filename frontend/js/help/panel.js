/* Panneau d'aide du back-office (V2-31, volet 3a).
 *
 * Un champ unique, une recherche tolérante (search.js), des résultats = question +
 * marche à suivre + bouton « M'y emmener » (deep-link). Ouvert par le bouton « Aide »
 * de l'en-tête (toutes les vues) ou le raccourci ⌘K / Ctrl+K.
 *
 * VETO ANDRÉ : jamais d'écran zéro-résultat. Une requête sans correspondance franche
 * affiche les rubriques les plus PROCHES, et en dernier repli « Voir toutes les
 * rubriques d'aide » (l'index entier). Chaque recherche est journalisée
 * (best-effort) — le taux de zéro-résultat mesure la santé de l'index.
 */

import { api } from "../api.js";
import { el, icon, mount, clear, refreshIcons } from "../ui.js";
import { navigate } from "../nav.js";
import { HELP_INDEX } from "./index.js";
import { searchHelp, resolveRoute } from "./search.js";

let overlayEl = null;      // instance ouverte (une seule à la fois)
let logTimer = null;       // journalisation différée (anti-rafale)

// ── Journalisation best-effort (santé de l'index) ────────────────────────────
// results_count = nombre de résultats FRANCS (0 si la recherche n'a rendu que des
// approches) : une recherche approchée est un « raté » pour la métrique de santé.
function scheduleLog(query, confidentCount) {
  clearTimeout(logTimer);
  const q = query.trim();
  if (q.length < 2) return;
  logTimer = setTimeout(() => {
    api.logHelpSearch(q, confidentCount).catch(() => { /* jamais bloquant */ });
  }, 700);
}

// ── Rendu d'une entrée d'aide ────────────────────────────────────────────────
function entryCard(entry) {
  const go = el("button", { class: "btn btn-sm btn-primary help-go" },
    icon("arrow-right", 15), entry.target.label || "M'y emmener");
  go.addEventListener("click", () => {
    closeHelpPanel();
    navigate(resolveRoute(entry.target.route));
  });
  return el("div", { class: "help-card" },
    el("div", { class: "help-q" }, icon("circle-help", 16), el("span", {}, entry.question)),
    el("ol", { class: "help-steps" }, ...entry.steps.map((s) => el("li", {}, s))),
    entry.exemple ? el("div", { class: "help-example" }, icon("lightbulb", 14),
      el("span", {}, entry.exemple)) : null,
    el("div", { class: "help-card-foot" }, go));
}

// La liste complète de l'index (dernier repli / mode exploration).
function fullIndexList(onPick) {
  return el("div", { class: "help-all" },
    el("div", { class: "help-all-title" }, "Toutes les rubriques d'aide"),
    el("div", { class: "help-all-grid" },
      ...HELP_INDEX.map((entry) => {
        const b = el("button", { class: "help-all-item" },
          icon("chevron-right", 14), el("span", {}, entry.question));
        b.addEventListener("click", () => onPick(entry));
        return b;
      })));
}

// ── Panneau ──────────────────────────────────────────────────────────────────
export function openHelpPanel() {
  if (overlayEl) { overlayEl.querySelector(".help-input")?.focus(); return; }

  const input = el("input", {
    type: "search", class: "help-input", placeholder: "Rechercher de l'aide… (ex. « envoyer le guide », « code wifi »)",
    "aria-label": "Rechercher de l'aide", autocomplete: "off", spellcheck: "false",
  });
  const results = el("div", { class: "help-results" });
  const panel = el("div", { class: "help-panel", role: "dialog", "aria-label": "Aide" },
    el("div", { class: "help-search" }, icon("search", 18), input,
      el("button", { class: "help-close", "aria-label": "Fermer l'aide", onClick: closeHelpPanel },
        icon("x", 20))),
    results,
    el("div", { class: "help-hint" }, "Astuce : ", el("kbd", {}, navigator.platform.includes("Mac") ? "⌘K" : "Ctrl+K"),
      " ouvre l'aide depuis n'importe quel écran."));
  const overlay = el("div", { class: "help-overlay" }, panel);
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeHelpPanel(); });
  document.body.append(overlay);
  overlayEl = overlay;
  refreshIcons();

  // Ouvre une entrée depuis la liste complète : on remonte et on la met en avant.
  const showSingle = (entry) => { mount(results, entryCard(entry)); results.scrollTop = 0; };

  function render() {
    const q = input.value;
    if (!q.trim()) {
      // Mode exploration : pas de recherche → l'index entier, jamais un écran vide.
      mount(results, fullIndexList(showSingle));
      return;
    }
    const { results: found, confident } = searchHelp(q);
    scheduleLog(q, confident ? found.length : 0);
    clear(results);
    if (!confident) {
      // Aucune correspondance franche → on ne rend JAMAIS le vide : approches +
      // repli vers la liste complète (§4, veto André).
      results.append(el("div", { class: "help-approx" }, icon("info", 15),
        el("span", {}, "Aucune correspondance exacte. Voici les rubriques les plus proches :")));
    }
    for (const r of found) results.append(entryCard(r.entry));
    const more = el("button", { class: "help-see-all" },
      icon("list", 15), "Voir toutes les rubriques d'aide");
    more.addEventListener("click", () => { input.value = ""; render(); input.focus(); });
    results.append(more);
    refreshIcons();
  }

  input.addEventListener("input", render);
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); closeHelpPanel(); } };
  overlay.addEventListener("keydown", onKey);
  render();
  setTimeout(() => input.focus(), 20);
}

export function closeHelpPanel() {
  if (!overlayEl) return;
  overlayEl.remove();
  overlayEl = null;
}

export function toggleHelpPanel() {
  overlayEl ? closeHelpPanel() : openHelpPanel();
}

/** Bouton « Aide » à monter dans l'en-tête (persistant sur toutes les vues). */
export function helpButton() {
  const btn = el("button", { class: "btn btn-sm btn-ghost help-open-btn", "aria-keyshortcuts": "Meta+K Control+K",
    title: "Aide (⌘K / Ctrl+K)", onClick: openHelpPanel },
    icon("life-buoy", 15), "Aide");
  return btn;
}

/** Installe le raccourci global ⌘K / Ctrl+K (une seule fois). */
export function initHelpShortcut() {
  if (window._casaHelpShortcut) return;
  window._casaHelpShortcut = (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
      e.preventDefault();
      toggleHelpPanel();
    }
  };
  document.addEventListener("keydown", window._casaHelpShortcut);
}
