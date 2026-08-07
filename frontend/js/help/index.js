/* Index d'aide du back-office (V2-31, volet 3a) — LA SOURCE DE VÉRITÉ.
 *
 * De la donnée, pas du code : chaque entrée décrit une question réelle du terrain
 * (les « c'est où ? » posés pendant le développement — docs/audit_ux.md §0), les
 * mots-clés du métier (synonymes compris : « clé wifi », « code boîte », « airbnb »),
 * une marche à suivre courte (2 à 4 étapes) et une destination (« M'y emmener »).
 *
 * Ton de rédaction = le MODÈLE de l'étape 1 de l'audit : une phrase qui dit ce que
 * le système FAIT et rassure sur l'erreur. Jamais un pavé.
 *
 * Le back-office est en français délibéré → l'index vit en FR. Il est structuré
 * pour être traduisible plus tard (clés `id` stables), mais reste HORS de
 * l'inventaire i18n voyageur (qui est le domaine du guide, pas du back-office).
 *
 * COUVERTURE GARANTIE PAR TEST : tout libellé visible du back-office (bouton, entrée
 * de menu, titre) doit être trouvé par la recherche → il doit apparaître dans la
 * `question` ou les `keywords` d'au moins une entrée. Ajouter un bouton sans
 * enrichir l'index fait ROUGIR la suite (frontend-tests/help-coverage.test.mjs).
 * Voir docs/aide.md pour la marche à suivre.
 *
 * `target.route` : gabarit de route existante (`:id` = logement courant, résolu par
 * le panneau contre le hash actif ; à défaut → « Mes logements »). Toute route doit
 * résoudre (vérifié par le test).
 *
 * `step` (V2-31, volet 3b) : le rang (1 à 7) de la CHAÎNE cible du guide
 * (docs/audit_ux.md §2) auquel la rubrique se rattache, ou `null` si elle relève
 * d'un usage HORS du chemin heureux (calendrier, abonnement, équipe, suppression).
 * La page « Premiers pas » (#/premiers-pas) range chaque rubrique sous son étape ;
 * les `null` tombent dans le bloc final « Et aussi ». Toute entrée porte un `step`
 * (1–7 ou null) — l'absence de champ est refusée par le test (aucune orpheline).
 */

export const HELP_INDEX = [
  {
    id: "envoyer-guide",
    step: 7,
    question: "Comment envoyer le guide à mon voyageur ?",
    keywords: ["envoyer le guide", "envoyer", "partager le guide", "email", "e-mail",
      "whatsapp", "sms", "qr", "qr code", "lien du guide", "holaguia", "envoi",
      "transmettre le guide", "adresser le guide"],
    steps: [
      "Depuis « Mes logements », sur un logement publié, cliquez « Envoyer le guide ».",
      "Choisissez le canal : email envoyé par Holaguia, WhatsApp, lien à copier ou QR à imprimer.",
      "Pour un séjour, l'email part dans la langue du locataire ; rien n'est envoyé tant que vous ne validez pas.",
    ],
    target: { route: "#/properties", label: "Mes logements" },
  },
  {
    id: "photo-couverture",
    step: 1,
    question: "Ajouter une photo de couverture au logement",
    keywords: ["photo de couverture", "couverture", "cover", "photo principale",
      "image du logement", "photo du logement", "vignette", "photo d'accueil"],
    steps: [
      "Ouvrez le guide du logement (porte « Guide Locataires »).",
      "Dans la fiche « Informations », choisissez une photo de couverture parmi vos photos.",
      "Elle illustre l'en-tête du guide et l'aperçu partagé (lien, email).",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "section-vide-guide",
    step: 5,
    question: "Pourquoi une rubrique vide n'apparaît-elle pas dans le guide ?",
    keywords: ["rubrique vide", "section vide", "n'apparaît pas", "invisible",
      "livraison de repas", "food delivery", "manquante", "pas affichée",
      "rubrique absente", "section masquée", "vide"],
    steps: [
      "Une rubrique sans contenu n'est pas montrée au voyageur : normal, on n'affiche pas une page blanche.",
      "Pour la faire apparaître, remplissez-la puis laissez la bascule « Visible dans le guide » cochée.",
      "Une rubrique masquée reste modifiable ici à tout moment.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "rattacher-bloc",
    step: null,
    question: "Rattacher un séjour en double (bloc en trop)",
    keywords: ["rattacher", "rattacher ce bloc", "bloc en double", "doublon",
      "double réservation", "bloc miroir", "chevauchement", "deux séjours identiques",
      "séjour en double", "fusionner"],
    steps: [
      "Dans le calendrier, modifiez les dates du séjour « maître ».",
      "Le bandeau de chevauchement propose « Rattacher ce bloc » avec le bon séjour.",
      "Confirmez : le double est absorbé (ses demandes et coordonnées sont reprises), jamais perdu.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "dates-ajustees",
    step: null,
    question: "Les dates que je corrige reviennent après la synchronisation",
    keywords: ["dates ajustées", "dates ≠ flux", "reprendre les dates du flux",
      "dates modifiées", "dates écrasées", "synchro écrase", "date revient",
      "airbnb change mes dates", "protéger les dates", "arrivée modifiée"],
    steps: [
      "Dès que vous déplacez une date d'un séjour importé, elle est protégée : la synchronisation ne l'écrase plus.",
      "Si la plateforme annonce d'autres dates, un signal sobre « Dates ≠ flux » vous prévient.",
      "Le bouton « Reprendre les dates du flux » réaligne sur la plateforme si vous le souhaitez.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "succession-fiche",
    step: null,
    question: "Ma fiche a disparu après une modification sur Vrbo / Airbnb",
    keywords: ["fiche à reprendre", "fiche disparue", "succession", "reprendre la fiche",
      "modification vrbo", "abritel", "réservation modifiée", "séjour vierge",
      "nouveau séjour vide", "fiche perdue"],
    steps: [
      "Quand une plateforme modifie une réservation, elle la réémet parfois comme un NOUVEAU séjour, vierge.",
      "Holaguia le détecte et propose « Reprendre la fiche » du séjour précédent (nom, contact, demandes).",
      "Un nouveau lien vivant sera envoyé : l'ancien lien de séjour, lui, ne fonctionne plus.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "code-boite-cles",
    step: 2,
    question: "Mettre un code de boîte à clés (et le changer pour un séjour)",
    keywords: ["code de boîte à clés", "boîte à clés", "keybox", "code boîte",
      "code d'accès", "code du logement", "code séjour", "clé", "coffre à clés",
      "digicode"],
    steps: [
      "Le code par défaut se saisit dans la rubrique « Boîte à clés » (espace sécurisé, chiffré).",
      "Pour un séjour précis, ouvrez-le dans le calendrier et renseignez un code spécifique.",
      "Le lien de séjour du locataire meurt 7 jours après le départ : le code aussi.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "langue-guide",
    step: 7,
    question: "Dans quelle langue le guide est-il envoyé au locataire ?",
    keywords: ["langue du locataire", "langue du guide", "langue d'envoi",
      "langue", "traduire l'envoi", "guest lang", "quelle langue", "langue email"],
    steps: [
      "Renseignez la « Langue du locataire » dans sa fiche de séjour (calendrier).",
      "Le guide et l'email de séjour partent alors dans cette langue.",
      "À défaut, la langue de repli est celle du guide (sa langue d'origine).",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "importer-calendrier",
    step: null,
    question: "Importer mon calendrier Airbnb / Abritel (iCal)",
    keywords: ["importer un calendrier", "flux de calendrier", "ical",
      "airbnb", "abritel", "vrbo", "booking", "url ical", "ajouter un flux",
      "ajouter & vérifier", "synchroniser airbnb", "lien du calendrier", "connecter"],
    steps: [
      "Sur la plateforme, copiez l'URL d'export iCal de votre logement.",
      "Dans « Calendrier », section « Flux de calendrier », collez l'URL et cliquez « Ajouter & vérifier ».",
      "Les réservations apparaissent et se mettent à jour à chaque synchronisation.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "synchroniser",
    step: null,
    question: "Synchroniser les réservations (fréquence, bouton)",
    keywords: ["synchroniser", "synchroniser maintenant", "synchronisation",
      "sync", "fréquence", "mise à jour des réservations", "rafraîchir le calendrier",
      "actualiser les séjours"],
    steps: [
      "Les flux importés se synchronisent automatiquement toutes les quelques heures.",
      "Le bouton « Synchroniser » du calendrier force une mise à jour immédiate.",
      "La synchronisation ne touche que les dates : vos saisies manuelles sont préservées.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "publier-guide",
    step: 6,
    question: "Publier ou dépublier mon guide",
    keywords: ["publier", "publier le guide", "dépublier", "mettre en ligne",
      "rendre public", "brouillon", "retirer le guide", "mise en ligne", "état du guide"],
    steps: [
      "Ouvrez le guide (porte « Guide Locataires ») puis cliquez « Publier le guide ».",
      "Le guide devient accessible via son lien public ; vous obtenez le lien et le QR.",
      "« Dépublier » le repasse en brouillon sans rien supprimer.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "valider-suggestions",
    step: 4,
    question: "Valider les lieux suggérés (rechercher les lieux)",
    keywords: ["lieux suggérés", "suggestions à valider", "valider", "à valider",
      "retenus", "rejetés", "approuver", "rejeter", "modifier", "rechercher les lieux",
      "enrichir", "enrichissement", "enrichissements", "tout approuver", "suggestions",
      "poi", "commerces", "restaurants", "lieux", "éditeur", "retour à l'éditeur",
      "aller à l'éditeur"],
    steps: [
      "Cliquez « Rechercher les lieux » : Holaguia repère commerces, plages, restaurants et urgences autour de l'adresse.",
      "Sur l'écran « Lieux suggérés », approuvez ou rejetez chaque lieu (ou « Tout approuver » par catégorie).",
      "Un lieu que vous avez validé n'est jamais réécrasé par une nouvelle recherche.",
    ],
    target: { route: "#/properties/:id/pois", label: "Lieux suggérés" },
  },
  {
    id: "coup-de-coeur",
    step: 4,
    question: "Ajouter un coup de cœur (un lieu personnel)",
    keywords: ["coup de cœur", "ajouter un lieu", "lieu favori", "recommandation",
      "mon restaurant préféré", "saisie manuelle", "ajouter au guide", "lieu personnel",
      "commentaire personnel"],
    steps: [
      "Sur l'écran « Lieux suggérés », cliquez « Ajouter un lieu ».",
      "Cherchez l'adresse, ajustez la catégorie et écrivez votre coup de cœur.",
      "Il apparaît en tête de sa catégorie dans le guide, avec votre commentaire.",
    ],
    target: { route: "#/properties/:id/pois", label: "Lieux suggérés" },
  },
  {
    id: "equipe-entretien",
    step: null,
    question: "L'espace « Équipe d'entretien » et son cahier (QR)",
    keywords: ["équipe d'entretien", "cahier de préparation", "ménage", "femme de ménage",
      "staff", "personnel", "cahier d'entretien", "lien du cahier", "qr équipe",
      "préparation", "réglages d'entretien", "règles d'entretien",
      "catalogue de demandes particulières", "draps", "welcome pack"],
    steps: [
      "La porte « Équipe d'entretien » d'un logement ouvre un cahier réservé à votre personnel.",
      "Il liste le planning des rotations et la préparation, sans jamais montrer le wifi, la boîte à clés ni la carte.",
      "Transmettez-le par son lien ou son QR ; c'est une fonction de l'offre Pro.",
    ],
    target: { route: "#/properties/:id/editor/staff", label: "Équipe d'entretien" },
  },
  {
    id: "medias-section",
    step: 5,
    question: "Ajouter des photos ou des documents à une rubrique",
    keywords: ["ajouter des photos", "photos", "documents", "pdf", "photo",
      "image", "illustrer", "photos et documents", "glisser-déposer", "téléverser",
      "joindre un document", "pièce jointe", "médias"],
    steps: [
      "Dans une rubrique de l'éditeur, ouvrez « Photos & documents ».",
      "Glissez-déposez ou cliquez pour ajouter des images (JPEG, PNG, WebP) ou un PDF.",
      "Ces médias illustrent la rubrique dans le guide du voyageur.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "wifi",
    step: 2,
    question: "Wifi : mot de passe et QR dans le guide",
    keywords: ["wifi", "wi-fi", "mot de passe wifi", "clé wifi", "code wifi",
      "réseau", "ssid", "qr wifi", "espace sécurisé", "connexion internet",
      "internet", "sensible"],
    steps: [
      "Dans la rubrique « Wifi », saisissez le nom du réseau et le mot de passe (espace sécurisé, chiffré).",
      "Vous pouvez ajouter plusieurs réseaux (maison, terrasse…).",
      "Le guide affiche un QR wifi : le voyageur se connecte sans taper le mot de passe.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "envoi-auto-j7",
    step: 7,
    question: "L'envoi automatique du guide à J-7 (et son interrupteur)",
    keywords: ["envoi automatique", "envoi auto", "j-7", "automatique", "7 jours avant",
      "interrupteur", "désactiver l'envoi automatique", "campagne", "avant l'arrivée",
      "envoyer avant"],
    steps: [
      "Pour un logement publié, le guide part automatiquement au locataire 7 jours avant son arrivée.",
      "L'email n'est envoyé qu'une fois : un envoi manuel préalable le remplace.",
      "L'interrupteur d'envoi automatique se règle dans la fiche « Informations » du logement.",
    ],
    target: { route: "#/properties/:id/editor", label: "Informations" },
  },
  {
    id: "abonnement",
    step: null,
    question: "Essai, abonnement et ajout d'un logement",
    keywords: ["abonnement", "mon abonnement", "offre", "essai", "changer d'offre",
      "offre actuelle", "passer en pro", "passer", "gratuit", "pro", "solo",
      "logements", "logements supplémentaires", "ajouter un logement", "payer",
      "paiement", "facturation", "gérer mon abonnement", "prix", "quota"],
    steps: [
      "La page « Mon abonnement » montre votre offre, vos jauges d'usage et le bouton pour en changer.",
      "L'offre Pro inclut plusieurs logements ; « Logements supplémentaires » en ajoute à la demande.",
      "Un changement vers une offre plus chère est immédiat ; vers une moins chère, à l'échéance.",
    ],
    target: { route: "#/abonnement", label: "Mon abonnement" },
  },
  {
    id: "demandes-voyageur",
    step: null,
    question: "Accepter ou refuser une demande du voyageur",
    keywords: ["demande du voyageur", "demande de service", "demande voyageur",
      "accepter", "refuser", "service à la demande", "requête du locataire",
      "demandes particulières", "sur demande"],
    steps: [
      "Quand un voyageur demande un service depuis le guide, un badge apparaît sur son séjour dans le calendrier.",
      "Ouvrez le séjour : acceptez ou refusez la demande.",
      "Une demande acceptée devient une intervention dans la préparation de l'équipe.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "voyageurs-enfants",
    step: null,
    question: "Voyageurs et âges des enfants (draps, welcome pack)",
    keywords: ["voyageurs", "nombre de voyageurs", "âges des enfants", "enfants",
      "lit bébé", "chaise haute", "draps", "welcome pack", "capacité",
      "compléter le séjour", "compléter", "combien de personnes"],
    steps: [
      "Renseignez le nombre de voyageurs et l'âge des enfants dans la fiche du séjour (calendrier).",
      "Ces informations dimensionnent la préparation (draps, welcome pack) et suggèrent l'équipement (lit bébé…).",
      "Les séjours incomplets sont regroupés en tête du calendrier pour un remplissage rapide.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
  {
    id: "visible-completee",
    step: 5,
    question: "Les bascules « Visible dans le guide » et « Section complétée »",
    keywords: ["visible dans le guide", "section complétée", "bascule", "interrupteur",
      "masquer une rubrique", "complétude", "avancement", "indicateur", "cocher",
      "case à cocher", "visible", "complétée"],
    steps: [
      "« Visible dans le guide » montre ou masque la rubrique aux voyageurs (elle reste modifiable ici).",
      "« Section complétée » indique que la rubrique est prête : c'est ce qui alimente votre indicateur d'avancement.",
      "Cocher « complétée » ne publie rien : c'est un repère pour vous.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "traductions",
    step: 6,
    question: "Traduire le guide (mettre à jour les traductions)",
    keywords: ["traductions", "traduire", "langues du guide", "traductions à jour",
      "à traduire", "périmé", "multilingue", "traduction", "anglais", "espagnol",
      "traduire le guide"],
    steps: [
      "Les traductions sont générées à la publication du guide et stockées.",
      "Après avoir modifié du contenu, le bouton « Traductions » signale ce qui est à retraduire.",
      "Cliquez-le pour mettre à jour les langues du guide.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "ajuster-position",
    step: 1,
    question: "Ajuster la position du logement sur la carte",
    keywords: ["ajuster la position", "position", "placer sur la carte", "carte",
      "géocodage", "localiser le logement", "point exact", "adresse mal placée",
      "position approximative", "déplacer le point"],
    steps: [
      "Ouvrez « Informations » puis la mini-carte de position.",
      "Déplacez le point exact du logement : les distances des lieux en dépendent.",
      "Une position placée à la main n'est jamais réécrasée par un géocodage.",
    ],
    target: { route: "#/properties/:id/editor", label: "Informations" },
  },
  {
    id: "informations-logement",
    step: 1,
    question: "Modifier les informations du logement (nom, adresse, contact)",
    keywords: ["informations", "fiche du logement", "nom du logement", "adresse",
      "contact", "modifier le logement", "changer l'adresse", "coordonnées",
      "heures d'arrivée", "heure de départ", "check-in", "check-out"],
    steps: [
      "Cliquez « Informations » sur la carte du logement ou dans l'éditeur.",
      "Modifiez le nom, l'adresse, le contact, les heures standard et la position.",
      "Si vous changez l'adresse, un re-géocodage n'a lieu qu'avec votre accord.",
    ],
    target: { route: "#/properties/:id/editor", label: "Informations" },
  },
  {
    id: "voir-guide",
    step: 6,
    question: "Prévisualiser le guide comme un voyageur",
    keywords: ["voir le guide", "aperçu", "prévisualiser", "ouvrir le guide",
      "tester le guide", "à quoi ressemble le guide", "consulter le guide", "prévisu"],
    steps: [
      "Sur un logement publié, cliquez « Voir le guide ».",
      "Le guide s'ouvre exactement comme le voit votre voyageur.",
      "Vous pouvez le partager par lien, QR, email ou WhatsApp.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "qr-imprimer",
    step: 6,
    question: "Imprimer l'affiche QR du guide",
    keywords: ["qr à imprimer", "affiche", "poster", "imprimer", "qr code à imprimer",
      "afficher dans le logement", "pdf du qr", "code à afficher", "qr code"],
    steps: [
      "Ouvrez le guide puis cliquez « QR à imprimer ».",
      "Choisissez la langue de l'affiche (le QR reste le même) et téléchargez le PDF.",
      "Affichez-le dans le logement : le voyageur scanne et accède au guide.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "copier-lien",
    step: 6,
    question: "Copier le lien du guide",
    keywords: ["copier le lien", "lien du guide", "partager le lien", "url du guide",
      "adresse du guide", "copier", "récupérer le lien", "lien public"],
    steps: [
      "Sur un logement publié, ouvrez le guide et cliquez « Copier le lien ».",
      "Le lien est prêt à coller (message, réseau, email).",
      "Ce lien public est permanent : c'est celui du QR imprimé dans la maison.",
    ],
    target: { route: "#/properties/:id/editor", label: "Ouvrir le guide" },
  },
  {
    id: "supprimer-logement",
    step: null,
    question: "Supprimer un logement",
    keywords: ["supprimer le logement", "supprimer", "effacer un logement",
      "corbeille", "retirer un logement", "supprimer définitivement"],
    steps: [
      "Sur la carte du logement, ouvrez le menu « ⋯ » puis « Supprimer le logement… ».",
      "Une confirmation nomme le logement et rappelle ce qui disparaît (guide, séjours, photos).",
      "L'action est irréversible : elle est volontairement sortie des gestes courants.",
    ],
    target: { route: "#/properties", label: "Mes logements" },
  },
  {
    id: "nouveau-logement",
    step: 1,
    question: "Créer un nouveau logement",
    keywords: ["nouveau logement", "créer un logement", "ajouter un logement",
      "premier logement", "commencer", "nouvelle propriété", "démarrer", "mes logements"],
    steps: [
      "Depuis « Mes logements », cliquez « Nouveau logement ».",
      "Saisissez le nom et l'adresse : elle sert à localiser le logement et à suggérer l'environnement.",
      "Ensuite, laissez Holaguia rechercher les lieux, puis complétez le guide.",
    ],
    target: { route: "#/properties", label: "Mes logements" },
  },
  {
    id: "calendrier-sejours",
    step: null,
    question: "Gérer le calendrier des séjours",
    keywords: ["calendrier", "calendrier des séjours", "séjours", "réservations",
      "nouveau séjour", "créer le séjour", "ajouter un séjour", "planning",
      "séjours annulés", "occupation", "réservation"],
    steps: [
      "La page « Calendrier » réunit tous vos séjours, importés et saisis à la main.",
      "« Nouveau séjour » ajoute une réservation directe ; les chevauchements et rotations sont signalés.",
      "Chaque séjour porte ses voyageurs, sa langue, ses demandes et son code de boîte à clés.",
    ],
    target: { route: "#/properties/:id/calendrier", label: "Calendrier" },
  },
];
