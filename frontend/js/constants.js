/* Métadonnées d'affichage des chapitres A→I (nom, icône Lucide, couleur).
   Les couleurs reprennent les map_color du seed poi_categories, agrégées par
   chapitre. Ordre d'affichage dans l'éditeur = ordre alphabétique du CdC §4. */

export const CHAPTERS = {
  A: { name: "Arrivée & départ",       icon: "door-open",        color: "var(--ch-A)" },
  B: { name: "Le logement",            icon: "house",            color: "var(--ch-B)" },
  C: { name: "Vie pratique",           icon: "shopping-basket",  color: "var(--ch-C)" },
  D: { name: "Urgences & santé",       icon: "heart-pulse",      color: "var(--ch-D)" },
  E: { name: "Services à la demande",  icon: "concierge-bell",   color: "var(--ch-E)" },
  F: { name: "Restaurants & sorties",  icon: "utensils",         color: "var(--ch-F)" },
  G: { name: "Activités & tourisme",   icon: "palmtree",         color: "var(--ch-G)" },
  H: { name: "Transports",             icon: "bus",              color: "var(--ch-H)" },
  I: { name: "Informations",           icon: "info",             color: "var(--ch-I)" },
};

export const CHAPTER_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H", "I"];

export function chapterMeta(ch) {
  return CHAPTERS[ch] || { name: ch, icon: "folder", color: "var(--muted)" };
}

/* Pays fréquents pour le formulaire de logement (le code ISO-2 reste la valeur
   stockée ; « Autre » laisse saisir un code libre). */
export const COUNTRIES = [
  ["ES", "Espagne"], ["FR", "France"], ["PT", "Portugal"], ["IT", "Italie"],
  ["GR", "Grèce"], ["DE", "Allemagne"], ["CH", "Suisse"], ["BE", "Belgique"],
  ["NL", "Pays-Bas"], ["GB", "Royaume-Uni"], ["MA", "Maroc"], ["HR", "Croatie"],
];

/* Libellés lisibles pour quelques options de select du field_schema (le seed
   stocke des valeurs techniques). */
export const OPTION_LABELS = {
  private: "Place privée",
  street: "Stationnement dans la rue",
  public: "Parking public",
};
