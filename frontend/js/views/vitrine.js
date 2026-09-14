/* Vitrine holaguia.com (V2-58, volet 1 de la revue graphique).

   Vraie page marketing (plus un formulaire de connexion) : héros, DÉMO VIVANTE (un
   vrai guide cliquable — le différenciateur du créneau), deux portes (hôtes /
   voyageurs), comment ça marche, différenciateurs, pied. La connexion quitte le
   centre → bouton sobre en haut à droite (#/login, l'espace propriétaire est intact).

   Identité Holaguia : crème/vert profond, serif des titres du guide (Fraunces),
   accent chaud terracotta (le soleil partagé avec Holaquetal Immo). Mobile-first,
   i18n FR/EN/ES (infra `microi18n`, structure prête pour les 7). */

import { api } from "../api.js";
import { el, icon, mount, refreshIcons } from "../ui.js";
import { navigate } from "../nav.js";
import { pickLang, setLang, translator, PUBLIC_LANGS } from "../microi18n.js";

const S = {
  fr: {
    login: "Connexion",
    hero_eyebrow: "Guides d'accueil numériques",
    hero_title: "Le guide d'accueil de votre location — ou de vos vacances",
    hero_sub: "Prêt en 10 minutes, en 7 langues, consultable hors-ligne.",
    cta_host: "Je suis hôte", cta_traveler: "Je pars en vacances",
    demo_eyebrow: "Démo vivante", demo_title: "Touchez un vrai guide",
    demo_sub: "Pas une maquette : un guide réel, cliquable, comme ceux que reçoivent vos voyageurs.",
    demo_open: "Ouvrir la démo en plein écran",
    doors_title: "Deux portes, un même soin",
    host_title: "Pour les hôtes",
    host_price: "Espace hôte",
    host_desc: "Un guide personnalisé de votre logement — arrivée, wifi, vos adresses préférées — que vous envoyez à vos locataires en un lien ou un QR.",
    host_cta: "Créer mon espace hôte",
    traveler_title: "Pour les voyageurs",
    traveler_desc: "Le guide des environs de n'importe quelle adresse de vacances : restaurants, plages, commerces, urgences. À vie, installable, hors-ligne.",
    traveler_cta: "Créer le guide de mon séjour",
    how_title: "Comment ça marche",
    how_host_1: "Saisissez l'adresse de votre logement.",
    how_host_2: "L'IA pré-remplit les environs ; vous validez et ajoutez VOS adresses.",
    how_host_3: "Envoyez le lien (ou le QR) à vos voyageurs.",
    how_trav_1: "Saisissez l'adresse de votre location de vacances.",
    how_trav_2: "Payez 2,90 € en quelques secondes.",
    how_trav_3: "Recevez votre guide par e-mail, installez-le, emportez-le hors-ligne.",
    diff_title: "Ce qui fait la différence",
    diff_langs: "7 langues", diff_offline: "Hors-ligne, installable",
    diff_sos: "Urgences locales", diff_map: "Carte interactive",
    diff_fast: "Généré en minutes",
    foot_osm: "Données cartographiques © OpenStreetMap",
    foot_login: "Espace propriétaire",
  },
  en: {
    login: "Log in",
    hero_eyebrow: "Digital welcome guides",
    hero_title: "The welcome guide for your rental — or for your holiday",
    hero_sub: "Ready in 10 minutes, in 7 languages, available offline.",
    cta_host: "I'm a host", cta_traveler: "I'm going on holiday",
    demo_eyebrow: "Live demo", demo_title: "Touch a real guide",
    demo_sub: "Not a mockup: a real, clickable guide, just like the ones your travellers receive.",
    demo_open: "Open the demo full screen",
    doors_title: "Two doors, the same care",
    host_title: "For hosts",
    host_price: "Host space",
    host_desc: "A personalised guide to your accommodation — arrival, wifi, your favourite spots — that you send to your guests with one link or QR code.",
    host_cta: "Create my host space",
    traveler_title: "For travellers",
    traveler_desc: "The guide to the area around any holiday address: restaurants, beaches, shops, emergencies. For life, installable, offline.",
    traveler_cta: "Create my stay's guide",
    how_title: "How it works",
    how_host_1: "Enter your accommodation's address.",
    how_host_2: "AI pre-fills the surroundings; you review and add YOUR spots.",
    how_host_3: "Send the link (or QR) to your guests.",
    how_trav_1: "Enter your holiday rental's address.",
    how_trav_2: "Pay €2.90 in seconds.",
    how_trav_3: "Receive your guide by e-mail, install it, take it offline.",
    diff_title: "What sets us apart",
    diff_langs: "7 languages", diff_offline: "Offline, installable",
    diff_sos: "Local emergencies", diff_map: "Interactive map",
    diff_fast: "Generated in minutes",
    foot_osm: "Map data © OpenStreetMap",
    foot_login: "Owner space",
  },
  es: {
    login: "Iniciar sesión",
    hero_eyebrow: "Guías de bienvenida digitales",
    hero_title: "La guía de bienvenida de tu alojamiento — o de tus vacaciones",
    hero_sub: "Lista en 10 minutos, en 7 idiomas, disponible sin conexión.",
    cta_host: "Soy anfitrión", cta_traveler: "Me voy de vacaciones",
    demo_eyebrow: "Demo en vivo", demo_title: "Toca una guía de verdad",
    demo_sub: "No es una maqueta: una guía real, navegable, como las que reciben tus viajeros.",
    demo_open: "Abrir la demo a pantalla completa",
    doors_title: "Dos puertas, el mismo cuidado",
    host_title: "Para anfitriones",
    host_price: "Espacio anfitrión",
    host_desc: "Una guía personalizada de tu alojamiento — llegada, wifi, tus direcciones favoritas — que envías a tus huéspedes con un enlace o un QR.",
    host_cta: "Crear mi espacio anfitrión",
    traveler_title: "Para viajeros",
    traveler_desc: "La guía de los alrededores de cualquier dirección de vacaciones: restaurantes, playas, comercios, emergencias. De por vida, instalable, sin conexión.",
    traveler_cta: "Crear la guía de mi estancia",
    how_title: "Cómo funciona",
    how_host_1: "Introduce la dirección de tu alojamiento.",
    how_host_2: "La IA rellena los alrededores; tú validas y añades TUS direcciones.",
    how_host_3: "Envía el enlace (o el QR) a tus huéspedes.",
    how_trav_1: "Introduce la dirección de tu alojamiento de vacaciones.",
    how_trav_2: "Paga 2,90 € en segundos.",
    how_trav_3: "Recibe tu guía por correo, instálala, llévala sin conexión.",
    diff_title: "Lo que marca la diferencia",
    diff_langs: "7 idiomas", diff_offline: "Sin conexión, instalable",
    diff_sos: "Emergencias locales", diff_map: "Mapa interactivo",
    diff_fast: "Generada en minutos",
    foot_osm: "Datos cartográficos © OpenStreetMap",
    foot_login: "Espacio propietario",
  },
};

function money(cts, currency) {
  try {
    return new Intl.NumberFormat("fr", { style: "currency",
      currency: (currency || "eur").toUpperCase() }).format((cts || 0) / 100);
  } catch (_) { return "2,90 €"; }
}

export function renderVitrine(root, params) {
  const lang = pickLang(params);
  const tr = translator(S, lang);
  let price = "2,90 €";

  // ── Barre supérieure : marque · langue · connexion (haut-droite) ──────────
  const langBtn = (code) => el("button", {
    class: "vt-lang" + (code === lang ? " on" : ""),
    onClick: () => { setLang(code); navigate("#/accueil?lang=" + code);
      renderVitrine(root, new URLSearchParams("lang=" + code)); },
  }, code.toUpperCase());
  const topbar = el("header", { class: "vt-top" },
    el("a", { class: "vt-brand", href: "#/accueil" },
      el("span", { class: "mark" }, icon("map-pinned", 20)), "Holaguia"),
    el("nav", { class: "vt-topnav" },
      el("div", { class: "vt-langs" }, ...PUBLIC_LANGS.map(langBtn)),
      el("a", { class: "vt-login", href: "#/login" }, icon("lock", 14), " ", tr("login"))));

  // ── Héros : promesse + CTA + téléphone démo ───────────────────────────────
  const phone = el("div", { class: "vt-phone" }, el("div", { class: "vt-phone-slot" },
    el("div", { class: "vt-dots" })));  // motif signature (pastilles) en attendant la démo
  const hero = el("section", { class: "vt-hero" },
    el("div", { class: "vt-hero-copy" },
      el("p", { class: "vt-eyebrow" }, tr("hero_eyebrow")),
      el("h1", { class: "vt-title" }, tr("hero_title")),
      el("p", { class: "vt-sub" }, tr("hero_sub")),
      el("div", { class: "vt-cta-row" },
        el("a", { class: "btn btn-primary vt-cta", href: "#/login" }, tr("cta_host")),
        el("a", { class: "btn vt-cta vt-cta-accent", href: "#/voyageur" },
          tr("cta_traveler") + " — " + price))),
    phone);

  // ── Deux portes ───────────────────────────────────────────────────────────
  const door = (klass, title, tag, desc, ctaLabel, href) =>
    el("div", { class: "vt-door " + klass },
      el("p", { class: "vt-door-tag" }, tag),
      el("h3", {}, title), el("p", { class: "vt-door-desc" }, desc),
      el("a", { class: "btn vt-door-cta", href }, ctaLabel));
  const doors = el("section", { class: "vt-section", id: "offres" },
    el("h2", { class: "vt-h2" }, tr("doors_title")),
    el("div", { class: "vt-doors" },
      door("host", tr("host_title"), tr("host_price"), tr("host_desc"),
        tr("host_cta"), "#/login"),
      door("traveler", tr("traveler_title"), price, tr("traveler_desc"),
        tr("traveler_cta"), "#/voyageur")));

  // ── Démo vivante ──────────────────────────────────────────────────────────
  const demoFrame = el("div", { class: "vt-demo-frame" });
  const demo = el("section", { class: "vt-section vt-demo", id: "demo" },
    el("p", { class: "vt-eyebrow center" }, tr("demo_eyebrow")),
    el("h2", { class: "vt-h2" }, tr("demo_title")),
    el("p", { class: "vt-demo-sub" }, tr("demo_sub")),
    demoFrame);

  // ── Comment ça marche (deux colonnes par audience) ────────────────────────
  const steps = (nums) => el("ol", { class: "vt-steps" },
    ...nums.map((k, i) => el("li", {}, el("span", { class: "vt-step-n" }, String(i + 1)),
      tr(k))));
  const how = el("section", { class: "vt-section", id: "comment" },
    el("h2", { class: "vt-h2" }, tr("how_title")),
    el("div", { class: "vt-how" },
      el("div", { class: "vt-how-col" }, el("h3", {}, tr("host_title")),
        steps(["how_host_1", "how_host_2", "how_host_3"])),
      el("div", { class: "vt-how-col" }, el("h3", {}, tr("traveler_title")),
        steps(["how_trav_1", "how_trav_2", "how_trav_3"]))));

  // ── Différenciateurs ──────────────────────────────────────────────────────
  const diffItem = (ic, label) => el("div", { class: "vt-diff" },
    icon(ic, 22), el("span", {}, label));
  const diff = el("section", { class: "vt-section vt-diffs" },
    el("h2", { class: "vt-h2 center" }, tr("diff_title")),
    el("div", { class: "vt-diff-row" },
      diffItem("languages", tr("diff_langs")),
      diffItem("wifi-off", tr("diff_offline")),
      diffItem("siren", tr("diff_sos")),
      diffItem("map", tr("diff_map")),
      diffItem("zap", tr("diff_fast"))));

  const footer = el("footer", { class: "vt-foot" },
    el("span", {}, tr("foot_osm")),
    el("a", { href: "#/login" }, tr("foot_login")));

  mount(root, el("div", { class: "vitrine" }, topbar, hero, doors, demo, how, diff, footer));
  refreshIcons();

  // ── Données dynamiques (prix, démo) — après le rendu, sans bloquer ────────
  api.guestOffer().then((o) => {
    price = money(o.price_cts, o.currency);
    root.querySelectorAll(".vt-cta-accent, .vt-door.traveler .vt-door-tag").forEach((n) => {
      if (n.classList.contains("vt-door-tag")) n.textContent = price;
      else n.textContent = tr("cta_traveler") + " — " + price;
    });
  }).catch(() => {});
  api.guestDemo().then((d) => {
    if (!d.token) return;
    const url = `/g/${encodeURIComponent(d.token)}`;
    // UN SEUL iframe (le téléphone du héros = le vrai guide démo, chargé en paresseux) :
    // la section « Démo vivante » n'ajoute qu'un lien plein écran → LCP léger.
    const f = document.createElement("iframe");
    f.src = url; f.loading = "lazy"; f.title = "Démo";
    phone.querySelector(".vt-phone-slot").replaceChildren(f);
    demoFrame.replaceChildren(
      el("a", { class: "btn btn-primary vt-demo-open", href: url, target: "_blank",
        rel: "noopener" }, tr("demo_open")));
    refreshIcons();
  }).catch(() => {});
}
