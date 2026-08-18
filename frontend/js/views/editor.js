/* Éditeur de guide (M-03) + repositionnement du logement (M-05).

   Navigation par chapitres A→I avec complétude par chapitre ; pour chaque
   section, formulaire généré depuis field_schema (voir components/dynform.js),
   sauvegarde par section (bouton + Cmd/Ctrl+S), visibilité et état « complété »,
   champs secrets chiffrés. Bandeau d'alerte + éditeur de position sur carte si
   le géocodage n'est pas au niveau « rooftop ». */

import { api, ApiError } from "../api.js";
import {
  el, icon, mount, clear, t, toast, openModal, confirmDialog, loadingBlock, refreshIcons,
} from "../ui.js";
import { navigate } from "../nav.js";
import { getOwner } from "../store.js";
import { handleQuotaError } from "../quota.js";
import { CHAPTER_ORDER, chapterMeta } from "../constants.js";
import { buildSectionForm } from "../components/dynform.js";
import { buildMediaPanel } from "../components/media.js";
import { openPropertyInfoModal, openPositionModal } from "../components/propertyinfo.js";
import { guideShareUrl } from "../share.js";
import { openShareMenu } from "../components/sharemenu.js";
import { openStaffShareMenu, staffUrl } from "../components/staffshare.js";
import { renderJourney, sectionHasSubstance, ESSENTIAL_CODES } from "../journey.js";
import { runEnrichment } from "./properties.js";

const ACCURACY_LABEL = { rooftop: "précise", street: "au niveau de la rue", city: "au centre de la commune" };

// Groupe distinct des sections « équipe d'entretien » (audience='staff', M-13).
const STAFF_META = { name: "Équipe d'entretien", icon: "clipboard-list", color: "#5B6B75" };
const isStaff = (s) => s.audience === "staff";

export async function renderEditor(view, pid, context) {
  // Séparation stricte des contextes (V2-26b) : l'éditeur est ouvert SOIT sur le
  // guide voyageur (sections audience='guest'), SOIT sur le cahier d'équipe
  // (audience='staff') — jamais les deux mélangés dans la même barre latérale.
  const isStaffCtx = context === "staff";
  const staffAccess = (getOwner() || {}).staff_access === true;
  // Garde de la porte staff (V2-18b) : un deep-link vers /editor/staff sans droit
  // (contournant la porte badgée et la bascule) reçoit le même encart d'upsell,
  // jamais l'accès — et ne peut donc pas se cogner au 402 à la sauvegarde.
  if (isStaffCtx && !staffAccess) {
    const msg = "Le cahier de l'équipe d'entretien est réservé à l'offre Pro.";
    handleQuotaError(new ApiError(402, msg, { code: "staff_locked", message: msg }));
    navigate(`#/properties/${pid}/editor`);
    return;
  }

  mount(view, el("div", { class: "page" }, loadingBlock("Ouverture de l'éditeur…")));

  let property, sectionsResp, secrets = {}, secretsAvailable = true;
  try {
    [property, sectionsResp] = await Promise.all([api.getProperty(pid), api.listSections(pid)]);
  } catch (err) {
    return mount(view, el("div", { class: "page" },
      el("div", { class: "errbox" }, err.message || "Logement introuvable.")));
  }
  try {
    secrets = (await api.getSecrets(pid)) || {};
  } catch (err) {
    secretsAvailable = false; // CASAGUIDE_SECRET_KEY absente (503) → 5.x non bloquant
  }

  const sections = sectionsResp.sections.map((s) => ({
    ...s,
    is_visible: s.is_visible == null ? true : s.is_visible,
    completed: !!s.completed,
  }));
  const byCode = new Map(sections.map((s) => [s.code, s]));
  const staffSections = sections.filter(isStaff);
  const guestSections = sections.filter((s) => !isStaff(s));
  // Les sections effectivement éditables dans ce contexte (voyageur ou staff).
  const ctxSections = isStaffCtx ? staffSections : guestSections;
  let current = ctxSections[0]?.code ?? sections[0]?.code;
  const expanded = new Set();

  // Une section est « garnie » sur sa SUBSTANCE, jamais sur une bascule
  // déclarative (V2-31 volet 2 : « Section complétée » retirée — c'est elle qui
  // nourrissait le % mensonger de l'audit).
  const filled = (s) => sectionHasSubstance(s.content, s.body_md);

  // ── Ossature de la page ───────────────────────────────────────────────────
  // Le « X % complété » de l'en-tête devient le FIL des 7 étapes (guest) ou un
  // décompte neutre de rubriques renseignées (cahier d'équipe).
  const progressSlot = el("div", { class: "editor-progress" });
  const statusBadge = el("span", { class: "badge badge-" + property.status });
  const headerRight = el("div", { class: "row" });
  let translationBtn = null;   // bouton « Traductions » (M-09), (re)créé au rendu
  const banner = el("div", {});
  const trBanner = el("div", { class: "tr-banner" });   // signal actif « traductions périmées » (V2-41)
  const sidebar = el("nav", { class: "card chapters", style: { padding: "10px" } });
  const panel = el("section", { class: "card section-panel" });
  // En-tête adapté au contexte (V2-26b) : le cahier d'équipe est un autre produit.
  const pageTitle = () => isStaffCtx ? `Cahier de préparation — ${property.name}` : property.name;
  const titleEl = el("h1", { class: "page-title", style: { margin: "0 0 6px" } }, pageTitle());
  const crumbName = el("span", {}, property.name);

  const page = el("div", { class: "page" },
    el("div", { class: "crumbs" },
      el("a", { href: "#/properties" }, "Mes logements"), icon("chevron-right", 14),
      crumbName),
    el("div", { class: "row", style: { justifyContent: "space-between", alignItems: "flex-start", marginBottom: "6px" } },
      el("div", {}, titleEl,
        el("div", { class: "row", style: { gap: "12px", alignItems: "center", flexWrap: "wrap" } },
          statusBadge, progressSlot)),
      headerRight),
    banner, trBanner,
    el("div", { class: "editor", style: { marginTop: "18px" } }, sidebar, panel));
  mount(view, page);

  renderHeaderActions();
  renderBanner();
  refreshProgress();
  if (current) { expanded.add(byCode.get(current).chapter); selectSection(current); }
  renderSidebar();
  // Contexte staff (V2-26) : amener le cahier sous les yeux (sur mobile la barre
  // latérale est empilée au-dessus du panneau).
  if (isStaffCtx && staffSections.length) {
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Cmd/Ctrl+S : sauvegarde la section active (nettoie l'ancien handler éventuel)
  if (window._casaSaveHandler) document.removeEventListener("keydown", window._casaSaveHandler);
  window._casaSaveHandler = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      if (!document.contains(panel)) { document.removeEventListener("keydown", window._casaSaveHandler); return; }
      e.preventDefault(); saveCurrent();
    }
  };
  document.addEventListener("keydown", window._casaSaveHandler);

  // Une entrée de section dans la barre latérale (mutualisée guest/staff).
  function sectionLink(s) {
    const done = filled(s);
    // Marquage impératif/facultatif (V2-31) : badge sobre « Essentiel » sur les
    // sections vitales (check-in/out, boîte à clés, accès, wifi) ; l'ABSENCE de
    // badge dit « à votre rythme » — pas besoin d'un second marqueur.
    const link = el("button", {
      class: "sec-link" + (s.code === current ? " on" : ""),
      onClick: () => selectSection(s.code),
    },
      el("span", { class: s.is_visible ? "" : "dim" }, t(s.name_i18n, s.code)),
      ESSENTIAL_CODES.has(s.code) ? el("span", { class: "badge badge-essential" }, "Essentiel") : null,
      s.is_sensitive ? icon("lock", 12) : null,
      icon("circle-check", 15, done));
    const tick = link.querySelector("[data-lucide]:last-child");
    if (tick) { tick.classList.add("tick"); if (!done) tick.classList.add("off"); }
    return link;
  }

  // Un chapitre repliable (tête + liste de sections). Le décompte reflète la
  // SUBSTANCE (rubriques garnies), plus la bascule déclarative supprimée.
  function chapterNode(key, meta, chSecs) {
    const done = chSecs.filter(filled).length;
    const isOpen = expanded.has(key);
    const head = el("button", { class: "chap-head", onClick: () => { isOpen ? expanded.delete(key) : expanded.add(key); renderSidebar(); } },
      el("span", { class: "chap-dot", style: { background: meta.color } }, icon(meta.icon, 15)),
      el("span", { class: "nm" }, meta.name),
      el("span", { class: "cnt" }, `${done}/${chSecs.length}`),
      icon(isOpen ? "chevron-down" : "chevron-right", 15));
    const node = el("div", { class: "chap" }, head);
    if (isOpen) node.append(el("div", { class: "sec-list" }, ...chSecs.map(sectionLink)));
    return node;
  }

  // ── Rendu de la barre latérale — STRICTEMENT un seul contexte (V2-26b) ──────
  // Contexte voyageur : chapitres A→I uniquement (le groupe « Équipe d'entretien »
  // n'apparaît plus ici — on n'y accède que par sa porte / la bascule).
  // Contexte staff : uniquement le cahier d'équipe + son bloc de transmission.
  function renderSidebar() {
    clear(sidebar);
    sidebar.append(contextSwitch());
    if (isStaffCtx) {
      if (staffSections.length) sidebar.append(chapterNode("S", STAFF_META, staffSections));
      sidebar.append(staffTransmitBlock());
    } else {
      for (const ch of CHAPTER_ORDER) {
        const chSecs = sections.filter((s) => s.chapter === ch && !isStaff(s));
        if (!chSecs.length) continue;
        sidebar.append(chapterNode(ch, chapterMeta(ch), chSecs));
      }
    }
    refreshIcons();
  }

  // Bascule sobre vers l'AUTRE contexte (V2-26b), gating respecté.
  function contextSwitch() {
    if (isStaffCtx) {
      return el("button", { class: "ctx-switch", onClick: () => navigate(`#/properties/${pid}/editor`) },
        icon("arrow-left-right", 15), el("span", {}, "Guide Locataires"));
    }
    return el("button", { class: "ctx-switch" + (staffAccess ? "" : " locked"),
      onClick: () => openStaffContext() },
      icon("arrow-left-right", 15), el("span", {}, "Équipe d'entretien"),
      staffAccess ? null : icon("lock", 12));
  }

  // Vers le contexte staff : accès → bascule ; sinon même encart d'invitation
  // que la porte badgée de la carte (patron staff_locked, jamais d'accès).
  function openStaffContext() {
    if (!staffAccess) {
      const msg = "Le cahier de l'équipe d'entretien est réservé à l'offre Pro.";
      handleQuotaError(new ApiError(402, msg, { code: "staff_locked", message: msg }));
      return;
    }
    navigate(`#/properties/${pid}/editor/staff`);
  }

  // Bloc de transmission du cahier (lien /s/ + ouverture + QR), dans la barre
  // latérale du contexte staff (staffshare mutualisé).
  function staffTransmitBlock() {
    const input = el("input", { type: "text", value: staffLink(), readonly: true, onFocus: (e) => e.target.select() });
    const copy = el("button", { class: "btn btn-sm", type: "button", onClick: async () => {
      try { await navigator.clipboard.writeText(staffLink()); toast("Lien du cahier copié.", "ok"); }
      catch (_) { toast("Copie impossible.", "err"); }
    } }, icon("link", 15), "Copier");
    return el("div", { class: "staff-transmit notice notice-info", style: { alignItems: "flex-start", marginTop: "12px" } },
      icon("clipboard-list", 18),
      el("div", { style: { flex: "1", minWidth: "0" } },
        el("b", {}, "Lien du cahier de préparation"),
        el("div", { class: "muted", style: { fontSize: "12px", margin: "3px 0 8px" } },
          "À partager avec votre équipe d'entretien uniquement. Accessible même avant publication ; ne contient jamais le wifi, la boîte à clés, ni la carte des lieux."),
        input,
        el("div", { class: "row", style: { gap: "6px", flexWrap: "wrap", marginTop: "6px" } }, copy,
          el("a", { class: "btn btn-sm", href: `/s/${property.staff_token}`, target: "_blank", rel: "noopener" }, icon("external-link", 15), "Ouvrir"),
          el("button", { class: "btn btn-sm", type: "button", onClick: () => openStaffShareMenu(property) },
            icon("qr-code", 15), "QR"),
          // Le calendrier des séjours nourrit la préparation de l'équipe (V2-23a).
          el("button", { class: "btn btn-sm", type: "button", onClick: () => navigate(`#/properties/${pid}/calendrier`) },
            icon("calendar-days", 15), "Calendrier"))));
  }

  function refreshProgress() {
    // Guide voyageur : le FIL des 7 étapes (V2-31), source unique côté serveur
    // (property.journey). Cahier d'équipe : un décompte NEUTRE de rubriques
    // garnies (le cahier n'est pas dans le parcours en 7 étapes) — jamais un « % ».
    clear(progressSlot);
    if (isStaffCtx) {
      const total = ctxSections.length;
      const done = ctxSections.filter(filled).length;
      progressSlot.append(el("span", { class: "muted", style: { fontSize: "13px" } },
        `${done}/${total} rubrique(s) renseignée(s)`));
      return;
    }
    const node = renderJourney(property.journey);
    if (node) progressSlot.append(node);
    refreshIcons();
  }

  // Recharge le fil après une écriture (une rubrique remplie, un secret posé…) :
  // seul le serveur fait foi (substance, POI, secrets, statut). Best-effort.
  async function refreshJourney() {
    try {
      const fresh = await api.getProperty(pid);
      property.journey = fresh.journey;
      refreshProgress();
    } catch (_) { /* le fil garde son dernier état connu */ }
  }

  // ── Panneau d'une section ─────────────────────────────────────────────────
  function selectSection(code) {
    current = code;
    const sec = byCode.get(code);
    const meta = isStaff(sec) ? STAFF_META : chapterMeta(sec.chapter);

    const form = buildSectionForm(sec, { secrets, propertyId: pid });

    const visibleSwitch = el("input", { type: "checkbox" });
    visibleSwitch.checked = sec.is_visible;
    const saveBtn = el("button", { class: "btn btn-primary" }, icon("save", 17), "Enregistrer");

    const secretUnavailable = form.hasSecrets && !secretsAvailable
      ? el("div", { class: "notice notice-warn", style: { marginBottom: "16px" } }, icon("triangle-alert", 18),
        el("div", {}, "Le stockage sécurisé n'est pas configuré sur le serveur (clé CASAGUIDE_SECRET_KEY) : les champs chiffrés ne pourront pas être enregistrés."))
      : null;

    mount(panel,
      el("div", { class: "sp-head" },
        el("span", { class: "chap-dot", style: { background: meta.color, width: "30px", height: "30px" } }, icon(sec.icon || meta.icon, 17)),
        el("div", {}, el("h2", {}, t(sec.name_i18n, sec.code)),
          el("div", { class: "row", style: { gap: "8px", marginTop: "4px" } },
            sec.is_sensitive ? el("span", { class: "badge badge-secret" }, icon("lock", 12), "Sensible") : null,
            sec.ai_enrichable ? el("span", { class: "badge badge-ai" }, icon("sparkles", 12), "Pré-remplissable IA") : null))),
      el("p", { class: "sp-desc" }, t(sec.description_i18n, "")),
      secretUnavailable,
      form.node,
      buildMediaPanel({ propertyId: pid, sectionCode: sec.code }).node,
      el("div", { class: "sp-toolbar" },
        // La bascule « Section complétée » a été RETIRÉE (V2-31 volet 2) : elle ne
        // pilotait plus rien — l'avancement se mesure désormais sur la SUBSTANCE
        // (le fil des étapes), jamais sur une déclaration. Seule « Visible dans le
        // guide » subsiste (elle décide de l'affichage voyageur, pas de complétude).
        el("div", { class: "sp-toggle" },
          el("label", { class: "switch" }, visibleSwitch, el("span", { class: "track" }), el("span", {}, "Visible dans le guide")),
          el("div", { class: "help switch-help" },
            "Décochez pour masquer cette rubrique aux voyageurs ; elle reste modifiable ici.")),
        el("span", { class: "spacer" }),
        el("span", { class: "savehint" }, el("kbd", {}, navigator.platform.includes("Mac") ? "⌘S" : "Ctrl+S")),
        saveBtn));

    saveBtn.addEventListener("click", () => saveCurrent({ visibleSwitch, form, saveBtn }));
    panel._ctx = { visibleSwitch, form, saveBtn };
    renderSidebar();
  }

  async function saveCurrent(ctx = panel._ctx) {
    if (!ctx) return;
    const { visibleSwitch, form, saveBtn } = ctx;
    const sec = byCode.get(current);
    const { content, body_md, hasSecrets, secretsPatch } = form.collect();
    saveBtn.disabled = true; saveBtn.textContent = "Enregistrement…";
    try {
      await api.putSection(pid, current, {
        // `completed` (colonne conservée, non destructif) : on renvoie sa valeur
        // existante inchangée — la bascule a disparu, la donnée n'est plus pilotée.
        content, body_md, is_visible: visibleSwitch.checked, completed: sec.completed,
      });
      if (hasSecrets && secretsAvailable) {
        Object.assign(secrets, secretsPatch);
        // Multi-wifi (M-15) : on renvoie la liste de réseaux (le PUT remplace
        // l'objet complet ; on conserve donc l'état des autres secrets en mémoire).
        await api.putSecrets(pid, {
          wifi_networks: secrets.wifi_networks || [],
          keybox_code: secrets.keybox_code || null, keybox_notes: secrets.keybox_notes || null,
        });
      } else if (hasSecrets && !secretsAvailable) {
        toast("Section enregistrée (champs chiffrés ignorés : stockage non configuré).", "err");
      }
      // Mise à jour de l'état local
      sec.content = content; sec.body_md = body_md;
      sec.is_visible = visibleSwitch.checked;
      if (!(hasSecrets && !secretsAvailable)) toast("Section enregistrée.", "ok");
      renderSidebar();
      // Le fil peut avancer (wifi/accès posés, contenu ajouté) → recompute serveur.
      refreshJourney();
      // Signal actif (V2-41) : le contenu a changé → une traduction peut être
      // périmée. On re-consulte le badge et, le cas échéant, on invite à retraduire
      // (un bandeau par sauvegarde, jamais de spam).
      maybePromptRetranslate();
    } catch (err) {
      // Refus lié au plan (essai expiré, quota, cahier équipe réservé au Pro —
      // staff_locked) → encart d'upsell propre plutôt qu'un toast brut (V2-18b).
      if (!handleQuotaError(err)) toast(err.message || "Enregistrement impossible.", "err");
    } finally {
      saveBtn.disabled = false; mount(saveBtn, icon("save", 17), "Enregistrer");
    }
  }

  // ── Actions d'en-tête (publier, voir le guide) ────────────────────────────
  function renderHeaderActions() {
    statusBadge.textContent = { draft: "Brouillon", published: "Publié", archived: "Archivé" }[property.status] || property.status;
    statusBadge.className = "badge badge-" + property.status;
    // Le badge de publication concerne le guide voyageur → masqué en contexte staff.
    statusBadge.style.display = isStaffCtx ? "none" : "";
    clear(headerRight);
    // Fiche du logement éditable (M-24) — accessible en permanence.
    headerRight.append(
      el("button", { class: "btn btn-sm", onClick: () => openInfo() },
        icon("home", 16), "Informations"));
    // Contexte staff (V2-26b) : les actions du guide voyageur (lien voyageur,
    // Traductions, QR à imprimer, Publier/Dépublier) n'ont pas de sens ici — on
    // ne montre que les outils de transmission du cahier d'équipe.
    if (isStaffCtx) {
      headerRight.append(
        el("a", { class: "btn btn-sm", href: `/s/${property.staff_token}`, target: "_blank", rel: "noopener" },
          icon("external-link", 16), "Ouvrir le cahier"),
        el("button", { class: "btn btn-sm", onClick: () => openStaffShareMenu(property) },
          icon("qr-code", 16), "Lien & QR de l'équipe"));
      refreshIcons();
      return;
    }
    if (property.status === "published") {
      // ── Traductions du guide (M-09) : bouton « Mettre à jour les traductions »
      // avec état (à jour / X éléments périmés). La (re)traduction est déclenchée
      // à la publication ; ce bouton rafraîchit après des modifications de contenu.
      translationBtn = el("button", { class: "btn btn-sm", onClick: () => runTranslate() },
        icon("languages", 16), el("span", { class: "tr-label" }, "Traductions"));
      headerRight.append(
        el("a", { class: "btn btn-sm", href: `/g/${property.guide_token}`, target: "_blank", rel: "noopener" },
          icon("external-link", 16), "Voir le guide"),
        el("button", { class: "btn btn-sm", onClick: () => openShareMenu(property) },
          icon("link", 16), "Copier le lien", icon("chevron-down", 14)),
        translationBtn,
        el("button", { class: "btn btn-sm", onClick: () => openPosterMenu() },
          icon("qr-code", 16), "QR à imprimer", icon("chevron-down", 14)),
        el("button", { class: "btn btn-sm", onClick: () => setStatus("draft") }, "Dépublier"));
      refreshTranslationState();
    } else {
      headerRight.append(
        el("button", { class: "btn btn-sm btn-primary", onClick: () => publish() }, icon("globe", 16), "Publier le guide"));
    }
    refreshIcons();
  }

  async function refreshTranslationState() {
    if (!translationBtn) return;
    try {
      const st = await api.translationStatus(pid);
      const label = translationBtn.querySelector(".tr-label");
      translationBtn.classList.remove("btn-warn");
      translationBtn.disabled = false;
      if (!st.total) {
        label.textContent = "Traductions";
        translationBtn.title = "Aucun texte à traduire pour le moment.";
      } else if (st.up_to_date) {
        label.textContent = "Traductions à jour";
        translationBtn.title = "Toutes les langues du guide sont à jour.";
      } else {
        label.textContent = `${st.outdated} à traduire`;
        translationBtn.classList.add("btn-warn");
        translationBtn.title = "Des contenus ont changé depuis la dernière traduction.";
      }
    } catch (_) { /* non bloquant */ }
  }

  async function runTranslate() {
    const label = translationBtn.querySelector(".tr-label");
    translationBtn.disabled = true;
    label.textContent = "Traduction…";
    try {
      await api.translate(pid);
      // La traduction s'exécute en tâche de fond : on sonde l'état quelques fois.
      for (let i = 0; i < 8; i++) {
        await new Promise((r) => setTimeout(r, 1200));
        const st = await api.translationStatus(pid);
        if (st.up_to_date) break;
      }
      toast("Traductions mises à jour.", "ok");
    } catch (err) {
      // Plafond de langues du plan atteint (402) → encart « changez d'offre ».
      if (!handleQuotaError(err)) toast(err.message || "Traduction impossible.", "err");
    } finally {
      translationBtn.disabled = false;
      refreshTranslationState();
      refreshIcons();
    }
  }

  // Signal actif « traductions périmées » (V2-41). Après une sauvegarde de section,
  // on re-consulte le badge : si des traductions sont périmées, on affiche un bandeau
  // non bloquant proposant de retraduire tout de suite. À la fin (ou si plus rien n'est
  // périmé), le bandeau disparaît sans recharger la page ; sinon le badge « X à
  // traduire » reste le rappel passif. Ne concerne que le guide voyageur publié.
  async function maybePromptRetranslate() {
    clear(trBanner);
    if (isStaffCtx || property.status !== "published") return;
    let st;
    try { st = await api.translationStatus(pid); }
    catch (_) { return; }   // non bloquant : l'échec du sondage ne gêne pas l'édition
    if (!st || !st.outdated) return;   // rien de périmé → aucun bandeau
    const retryBtn = el("button", { class: "btn btn-sm btn-primary" },
      icon("languages", 16), "Retraduire maintenant");
    retryBtn.addEventListener("click", async () => {
      retryBtn.disabled = true; mount(retryBtn, icon("loader", 16), "Retraduction…");
      refreshIcons();
      await runTranslate();          // action de traduction existante (même que le bouton)
      maybePromptRetranslate();      // ré-évalue : bandeau retiré si à jour, sinon reste
    });
    trBanner.append(el("div",
      { class: "notice notice-warn", style: { marginTop: "14px", alignItems: "center" } },
      icon("languages", 18),
      el("div", { style: { flex: "1" } },
        el("b", {}, "Ce contenu a changé — "),
        "des traductions sont périmées."),
      retryBtn));
    refreshIcons();
  }

  // Lien du cahier d'équipe /s/{staff_token} (utilisé par le bloc de transmission).
  function staffLink() { return staffUrl(property); }

  // Petit menu de langue du poster QR (M-26) : FR / EN / ES. Le poster ne sort
  // plus qu'en une langue, choisie par le propriétaire.
  const POSTER_LANGS = [["fr", "Français"], ["en", "English"], ["es", "Español"]];
  function openPosterMenu() {
    const body = el("div", {},
      el("p", { class: "muted", style: { marginTop: 0 } },
        "Langue de l'affiche à imprimer (le QR reste le même) :"),
      el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } },
        ...POSTER_LANGS.map(([code, label]) =>
          el("button", { class: "btn", onClick: () => { menu.close(); downloadPoster({ lang: code }); } },
            icon("qr-code", 16), label))));
    const menu = openModal({ title: "QR à imprimer", body,
      footer: [el("button", { class: "btn btn-ghost", type: "button", onClick: () => menu.close() }, "Fermer")] });
  }

  // Téléchargement de l'affiche QR imprimable (M-07, M-26). Le PDF est protégé
  // (owner) : on le récupère avec le jeton puis on déclenche le téléchargement.
  async function downloadPoster({ size, lang } = {}) {
    try {
      const blob = await api.posterBlob(pid, { size, lang });
      const url = URL.createObjectURL(blob);
      const suffix = lang ? `-${lang}` : "";
      const a = el("a", { href: url, download: `holaguia-qr-${property.name || pid}${suffix}.pdf` });
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      toast("Affiche QR téléchargée.", "ok");
    } catch (err) { toast(err.message || "Génération du PDF impossible.", "err"); }
  }

  async function publish() {
    // État de préparation SOUPLE (audit 0.2 : le fil guide, le propriétaire
    // décide). Seule l'absence d'adresse bloque réellement (sans elle, ni carte
    // ni distances) — comme aujourd'hui. Tout le reste AVERTIT, jamais un mur.
    if (property.lat == null) {
      toast("Ajoutez d'abord l'adresse du logement : sans elle, le guide n'a ni carte ni distances.", "err");
      openInfo();
      return;
    }
    // Manques des étapes 1 & 2, en langage humain (fournis par le serveur).
    const steps = (property.journey && property.journey.steps) || [];
    const missing = steps.filter((s) => s.n === 1 || s.n === 2).flatMap((s) => s.missing || []);
    if (!(await confirmPublish(missing))) return;
    await setStatus("published");
    const link = guideShareUrl(property);
    openModal({
      title: "Guide publié 🎉",
      body: el("div", {},
        el("p", { class: "muted", style: { marginTop: 0 } }, "Partagez ce lien (ou son QR code) avec vos voyageurs :"),
        el("div", { class: "field" }, el("input", { type: "text", value: link, readonly: true, onFocus: (e) => e.target.select() })),
        el("div", { class: "row", style: { gap: "8px", flexWrap: "wrap" } },
          el("a", { class: "btn btn-sm", href: `/g/${property.guide_token}`, target: "_blank", rel: "noopener" }, icon("external-link", 16), "Ouvrir le guide"),
          el("button", { class: "btn btn-sm", type: "button", onClick: () => openPosterMenu() }, icon("qr-code", 16), "QR code à imprimer"))),
      footer: [el("button", { class: "btn btn-primary", onClick: (e) => { navigator.clipboard?.writeText(link); toast("Lien copié.", "ok"); } }, "Copier le lien")],
    });
  }

  // Confirmation de publication qui LISTE les manques (jamais un mur, audit 0.2) :
  // « Le wifi n'est pas renseigné — votre voyageur le cherchera » + « Publier quand
  // même » toujours disponible. Sans manque : confirmation simple.
  function confirmPublish(missing) {
    return new Promise((resolve) => {
      const body = missing.length
        ? el("div", {},
            el("p", { class: "muted", style: { marginTop: 0 } },
              "Votre guide peut être publié, mais quelques indispensables manquent encore :"),
            el("ul", { class: "publish-missing" }, ...missing.map((m) => el("li", {}, m))),
            el("p", { class: "muted", style: { marginBottom: 0 } },
              "Vous pourrez les compléter à tout moment."))
        : el("p", { class: "muted", style: { margin: 0 } },
            "Rendre ce guide accessible via son lien public ? Les voyageurs pourront le consulter.");
      const ok = el("button", { class: "btn btn-primary" }, missing.length ? "Publier quand même" : "Publier");
      const cancel = el("button", { class: "btn btn-ghost" }, "Annuler");
      const m = openModal({ title: "Publier le guide", body, footer: [cancel, ok],
        onClose: () => resolve(false) });
      ok.addEventListener("click", () => { resolve(true); m.close(); });
      cancel.addEventListener("click", () => { resolve(false); m.close(); });
    });
  }

  async function setStatus(status) {
    try {
      const updated = await api.updateProperty(pid, { status });
      property = { ...property, ...updated };
      renderHeaderActions();
      refreshProgress();   // le fil reflète la (dé)publication (étape 6)
      toast(status === "published" ? "Guide publié." : "Guide repassé en brouillon.", "ok");
    } catch (err) { toast(err.message || "Action impossible.", "err"); }
  }

  // ── Fiche du logement + position (M-24, M-05) ─────────────────────────────
  // La modale « Informations » et la mini-carte de position sont mutualisées
  // (components/propertyinfo.js) et accessibles À TOUT MOMENT.
  function applyPropertyUpdate(updated) {
    property = { ...property, ...updated };
    titleEl.textContent = pageTitle();
    crumbName.textContent = property.name;
    renderBanner();
    renderHeaderActions();
    // Adresse/contact/couverture modifiés → le fil (étape 1) peut avancer. Si la
    // réponse ne porte pas le fil (modale position), on le recharge du serveur.
    if (updated && updated.journey) { property.journey = updated.journey; refreshProgress(); }
    else refreshJourney();
  }

  function openInfo() {
    openPropertyInfoModal(property, { onSaved: applyPropertyUpdate });
  }

  function openPosition() {
    openPositionModal(property, { onSaved: applyPropertyUpdate });
  }

  // ── Bandeau de position (M-05) ────────────────────────────────────────────
  function renderBanner() {
    clear(banner);
    if (property.lat == null) {
      banner.append(el("div", { class: "notice notice-warn", style: { marginTop: "14px", alignItems: "center" } }, icon("map-pin-off", 18),
        el("div", { style: { flex: "1" } }, el("b", {}, "Logement non localisé. "),
          "Lancez l'enrichissement pour géocoder l'adresse, ou placez le point manuellement. ",
          el("a", { href: "#", onClick: (e) => { e.preventDefault(); runEnrichment(pid, "initial", { onFinished: () => reloadProperty() }); } }, "Enrichir maintenant")),
        el("button", { class: "btn btn-sm", onClick: () => openPosition() }, icon("map-pin", 16), "Placer sur la carte")));
      refreshIcons();
      return;
    }
    // Bandeau d'ALERTE quand la position est douteuse (M-24 : ce n'est plus la
    // seule porte vers la carte — « Ajuster la position » est toujours dispo).
    if (property.geocode_accuracy && property.geocode_accuracy !== "rooftop") {
      const btn = el("button", { class: "btn btn-sm", onClick: () => openPosition() }, icon("move", 16), "Ajuster la position");
      banner.append(el("div", { class: "notice notice-warn", style: { marginTop: "14px", alignItems: "center" } }, icon("map-pin", 18),
        el("div", { style: { flex: "1" } }, el("b", {}, "Position approximative "),
          `(localisation ${ACCURACY_LABEL[property.geocode_accuracy] || property.geocode_accuracy}). `,
          "Ajustez le point exact sur la carte pour des distances fiables."),
        btn));
    }
    refreshIcons();
  }

  async function reloadProperty() {
    try { property = await api.getProperty(pid); applyPropertyUpdate(property); } catch (_) {}
  }
}
