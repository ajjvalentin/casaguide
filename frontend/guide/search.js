/* Recherche par mots-clés DANS le guide voyageur (V2-33 volet 2).
 *
 * Entièrement HORS-LIGNE : l'index est construit au chargement depuis le DOM déjà
 * rendu (titres de sections, corps, noms/descriptions/commentaires de POI, badge du
 * jour de marché) — aucun réseau, aucune donnée qui ne soit déjà à l'écran. La
 * technique de correspondance tolérante (fautes de frappe, 7 langues) vient du cœur
 * partagé `js/lib/matchcore.js` (le même que l'aide ⌘K du back-office).
 *
 * Secrets : le wifi et le code de boîte à clés déchiffrés (injectés APRÈS coup par
 * `initSecrets` dans des `.secret-slot`) ne rentrent JAMAIS dans l'index — on
 * construit avant, et on retire explicitement ces éléments à la moisson. Seuls les
 * libellés PUBLICS (titres « Wifi », « Boîte à clés ») sont trouvables, et mènent à
 * la section.
 *
 * Sans JS : rien n'est injecté → aucun champ, la page reste intacte (doctrine M-08).
 * Toute modif de ce fichier impose de le PRÉ-CACHER dans sw.js (SHELL_ASSETS) et de
 * bumper la VERSION du cache, sinon la recherche meurt hors-ligne.
 */

import { buildIndex, search } from "../js/lib/matchcore.js";

const DEBOUNCE_MS = 140;
const MIN_QUERY = 2;

function txt(node) {
  return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
}

// Texte public d'une section : on CLONE puis on retire les secrets et les contrôles
// (boutons, slots de secrets, formulaire « demander ce service ») avant de moissonner
// — le mot de passe wifi / le code de boîte à clés ne peuvent pas fuiter dans l'index.
function sectionText(card) {
  const clone = card.cloneNode(true);
  clone.querySelectorAll(
    ".secret-slot, .secret-card, .svc-request, .copy-btn, button, script, style",
  ).forEach((n) => n.remove());
  return txt(clone);
}

// Construit les entrées d'index depuis le DOM rendu (sections + POI de tous les onglets).
function collectEntries(root) {
  const entries = [];
  root.querySelectorAll(".sec-card[id]").forEach((card) => {
    const title = txt(card.querySelector("h3"));
    if (!title && !card.textContent.trim()) return;
    entries.push({ type: "section", title, el: card, parts: [title, sectionText(card)] });
  });
  root.querySelectorAll(".poi-card").forEach((card) => {
    const name = txt(card.querySelector("h4"));
    if (!name) return;
    const cat = card.closest(".cat");
    const catTitle = cat ? txt(cat.querySelector(".cat-title")).replace(/·.*$/, "").trim() : "";
    entries.push({
      type: "poi", title: name, el: card,
      context: catTitle,
      parts: [name, txt(card.querySelector(".fav")), txt(card.querySelector(".prose")),
        txt(card.querySelector(".hours")), txt(card.querySelector(".market-day")), catTitle],
    });
  });
  return entries;
}

// Amène un résultat à l'écran « comme le ferait un clic » : bon onglet, listes
// dépliées/filtre levé si le POI y était masqué, défilement doux + surbrillance.
function reveal(el) {
  const panel = el.closest(".tab-panel[data-tab]");
  if (panel && window._activateTab) window._activateTab(panel.dataset.tab);
  const cat = el.closest(".cat");
  if (cat) {
    // Filtre par cuisine actif qui masquerait la carte → revenir à « Tout ».
    const allChip = cat.querySelector('.cuisines .cchip[data-cuisine=""]');
    if (allChip && !allChip.classList.contains("on")) allChip.click();
    // Liste repliée à 4 → déplier pour révéler une carte au-delà du seuil.
    const more = cat.querySelector(".more-btn");
    if (more && more.classList.contains("show") && more.dataset.less
        && more.textContent !== more.dataset.less) {
      more.click();
    }
  }
  setTimeout(() => {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("search-hit");
    setTimeout(() => el.classList.remove("search-hit"), 2400);
  }, 60);
}

export function initSearch() {
  const main = document.getElementById("content");
  const tabs = document.querySelector(".guide-tabs");
  // Structure du guide voyageur requise (les pages /s/ « équipe » n'ont pas d'onglets).
  if (!main || !tabs) return;

  const entries = collectEntries(main);
  if (!entries.length) return;

  const index = buildIndex(entries, { textOf: (e) => e.parts });

  const d = document.body.dataset;
  const placeholder = d.searchPh || "Rechercher dans le guide";
  const noneLabel = d.searchNone || "Aucun résultat exact — suggestions :";
  const clearLabel = d.searchClear || "Effacer";

  const input = document.createElement("input");
  input.type = "search";
  input.className = "gs-input";
  input.setAttribute("placeholder", placeholder);
  input.setAttribute("aria-label", placeholder);
  input.setAttribute("autocomplete", "off");
  input.setAttribute("enterkeyhint", "search");

  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "gs-clear";
  clearBtn.setAttribute("aria-label", clearLabel);
  clearBtn.textContent = "×";
  clearBtn.hidden = true;

  const results = document.createElement("div");
  results.className = "gs-results";
  results.hidden = true;

  const box = document.createElement("div");
  box.className = "guide-search";
  const field = document.createElement("div");
  field.className = "gs-field";
  field.append(input, clearBtn);
  box.append(field, results);
  tabs.parentNode.insertBefore(box, tabs);

  let hits = [];

  function close() {
    results.hidden = true;
    results.replaceChildren();
  }

  function render(query) {
    const q = query.trim();
    clearBtn.hidden = !q;
    if (q.length < MIN_QUERY) { close(); return; }
    const { results: found, confident } = search(index, q, { limit: 8 });
    hits = found;
    results.replaceChildren();
    if (!found.length) { close(); return; }   // index vide (jamais en pratique)
    if (!confident) {
      const note = document.createElement("div");
      note.className = "gs-note";
      note.textContent = noneLabel;
      results.append(note);
    }
    for (const r of found) results.append(row(r.entry));
    results.hidden = false;
  }

  function row(entry) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "gs-hit gs-hit-" + entry.type;
    const ic = document.createElement("span");
    ic.className = "gs-ic";
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = entry.type === "poi" ? "📍" : "📄";
    const label = document.createElement("span");
    label.className = "gs-label";
    label.textContent = entry.title;
    btn.append(ic, label);
    if (entry.type === "poi" && entry.context) {
      const ctx = document.createElement("span");
      ctx.className = "gs-ctx";
      ctx.textContent = entry.context;
      btn.append(ctx);
    }
    btn.addEventListener("click", () => { close(); input.blur(); reveal(entry.el); });
    return btn;
  }

  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => render(input.value), DEBOUNCE_MS);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { input.value = ""; render(""); input.blur(); }
    else if (e.key === "Enter") {
      e.preventDefault();
      clearTimeout(timer);
      render(input.value);
      const first = results.querySelector(".gs-hit");
      if (first) first.click();
    }
  });
  clearBtn.addEventListener("click", () => { input.value = ""; render(""); input.focus(); });
  // Clic hors de la boîte → replier les résultats (le champ reste).
  document.addEventListener("click", (e) => {
    if (!box.contains(e.target)) close();
  });
}
