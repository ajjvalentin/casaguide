# Cahier des charges — « Guide du logement de vacances » (nom de code : *CasaGuide*)

**Version :** 1.0 — Brouillon de travail
**Date :** 11 juillet 2026
**Statut :** À valider par le porteur de projet

---

## 1. Présentation du projet

### 1.1 Contexte
Les propriétaires de logements de vacances (Airbnb, Booking, gestion directe) transmettent aujourd'hui les informations pratiques de manière dispersée : messages, PDF, classeurs papier dans le logement. Résultat : questions répétitives des voyageurs, informations obsolètes, expérience d'accueil inégale.

### 1.2 Objectif
Créer une plateforme SaaS permettant à des propriétaires de générer, pour chaque logement, un **guide d'accueil numérique complet, interactif et multilingue**, accessible aux voyageurs via un lien ou un QR code, avec :
- une **liste pré-définie de sections** que le propriétaire complète (rien n'est oublié) ;
- un **enrichissement automatique par IA** à partir de l'adresse du logement (hôpital, restaurants, marchés, etc.) que le propriétaire valide ;
- une **carte interactive** avec distances et temps de trajet depuis le logement.

### 1.3 Proposition de valeur
- **Propriétaire :** gain de temps (moins de questions), image professionnelle, guide toujours à jour, création assistée en quelques minutes.
- **Voyageur :** toutes les informations au même endroit, sur son téléphone, dans sa langue, consultable hors-ligne.

---

## 2. Acteurs et personas

| Acteur | Description | Besoins clés |
|---|---|---|
| **Propriétaire / Gestionnaire** | Particulier (1–3 logements) ou conciergerie (10–100 logements) | Création rapide, duplication entre logements, mise à jour simple |
| **Voyageur** | Locataire du logement, souvent étranger, sur mobile | Accès immédiat sans installation, langue maternelle, hors-ligne |
| **Administrateur plateforme** | Exploitant du SaaS | Supervision, support, facturation, modération |

---

## 3. Périmètre fonctionnel

### 3.1 Back-office propriétaire (web responsive)

**Compte et logements**
- Inscription / connexion (email + mot de passe, OAuth Google en option)
- Gestion multi-logements sous un même compte (adapté aux conciergeries)
- Duplication d'un guide existant vers un nouveau logement
- Rôles : propriétaire principal, co-gestionnaire (V2)

**Création du guide**
- Saisie de l'adresse du logement → géocodage automatique
- **Formulaire guidé** basé sur la liste pré-définie des sections (§4) avec indicateur de complétude (ex. « guide complété à 78 % »)
- Chaque section : champs structurés + zone de texte libre + photos
- Possibilité de masquer une section non pertinente (ex. pas de piscine)
- Ajout de sections personnalisées

**Enrichissement automatique (IA)**
- Déclenché à la saisie de l'adresse : la plateforme propose un brouillon des sections « environnement » (santé, urgences, commerces, restaurants, marchés, activités, transports)
- Chaque suggestion est présentée avec sa source et sa distance ; le propriétaire **valide, modifie ou rejette** chaque élément (jamais de publication automatique)
- Bouton « rafraîchir les suggestions » (avec quota selon l'abonnement)

**Publication et partage**
- Génération d'un lien unique non-devinable : `guide.app.com/g/{token}`
- Génération du **QR code** (téléchargeable en PDF prêt à imprimer / encadrer)
- Options d'accès : lien ouvert / code PIN / accès limité aux dates du séjour (V2)
- Prévisualisation du guide tel que le verra le voyageur
- Statistiques de consultation (nombre de visites, sections les plus vues)

### 3.2 Guide voyageur (PWA mobile-first)

- Accès via lien ou scan du QR code, **sans création de compte ni installation**
- Installable comme PWA (icône sur l'écran d'accueil), **consultable hors-ligne** après première visite (crucial avant configuration du wifi)
- Navigation par sections avec recherche interne
- **Sélecteur de langue** : contenu traduit automatiquement (FR/EN/ES/DE/NL au minimum)
- **Carte interactive** (§6) : logement au centre, POI par catégorie, distances et temps de trajet
- Liens actionnables : appel direct (tél. urgences, taxi), ouverture dans Google Maps / Apple Plans pour l'itinéraire, liens web des établissements
- Boutons de copie rapide (mot de passe wifi, code boîte à clés)
- Bouton « contacter le propriétaire » (tél / WhatsApp / email selon préférence du propriétaire)
- Signalement d'une information obsolète (remonte au propriétaire) (V2)

### 3.3 Administration plateforme

- Tableau de bord : comptes, logements, consommation API, revenus
- Gestion des abonnements et de la facturation (Stripe)
- Outils de support (accès en lecture au compte d'un propriétaire avec son accord)
- Suivi des coûts d'enrichissement (appels API géo + IA par logement)

---

## 4. Liste pré-définie des sections du guide

> **C'est le cœur du produit.** Cette checklist garantit qu'aucune information n'est oubliée. Chaque section indique : les champs structurés, la part remplie par le propriétaire (P) et celle pré-remplie par l'IA (IA, toujours validée par P).

### A. Arrivée et départ
| Élément | Champs | Source |
|---|---|---|
| Check-in | Heure au plus tôt, procédure, arrivée autonome ou accueil | P |
| Check-out | Heure limite, consignes de départ (clés, poubelles, vaisselle, linge) | P |
| Boîte à clés | Emplacement (photo), **code** (donnée sensible), instructions | P |
| Accès au logement | Adresse exacte, étage, interphone, portail, photo de la façade | P + IA (géocodage) |
| Parking | Place privée / rue / parking public le plus proche (tarifs, lien) | P + IA |
| Arriver depuis l'aéroport / la gare | Itinéraires recommandés, navettes | IA + P |

### B. Le logement
| Élément | Champs | Source |
|---|---|---|
| Wifi | Nom du réseau (SSID), **mot de passe**, emplacement de la box, QR de connexion auto | P |
| Équipements | Climatisation, chauffage, lave-linge, lave-vaisselle, TV, four, cafetière… : mode d'emploi de chacun (texte + photos, PDF notices en option) | P |
| Piscine / jacuzzi / barbecue | Règles d'usage, horaires, sécurité enfants | P |
| Compteurs et disjoncteurs | Emplacements (eau, électricité, gaz), que faire en cas de coupure | P |
| Règlement intérieur | Non-fumeur, animaux, fêtes, bruit (heures de silence, réglementation locale), nombre max d'occupants, zones interdites | P + IA (réglementation locale sur le bruit) |
| Ménage et linge | Consignes, où trouver draps/serviettes, produits fournis | P |

### C. Vie pratique
| Élément | Champs | Source |
|---|---|---|
| **Poubelles et tri** | Emplacement des conteneurs (carte + photo), jours/horaires de collecte, consignes de tri locales | P + IA |
| Supermarchés et commerces | Les plus proches, horaires, distance, lien web | IA |
| **Marchés locaux** | Jours, horaires, emplacement (carte), spécialités | IA + P |
| Boulangerie, pharmacie, tabac, poste, distributeur de billets | Plus proches, horaires | IA |
| **Centres commerciaux** | Nom, distance, horaires, **lien internet** | IA |
| Laverie | La plus proche si pas de lave-linge | IA |

### D. Urgences et santé
| Élément | Champs | Source |
|---|---|---|
| **Numéros d'urgence** | 112 (UE), urgences médicales, pompiers, **police locale / Guardia Civil**, numéro contre les violences, urgences vétérinaires | IA (selon pays) |
| **Hôpital le plus proche** | Nom, adresse, distance, téléphone, service d'urgences 24 h ? | IA |
| Pharmacie de garde | Pharmacie la plus proche + lien vers le planning des gardes | IA |
| Médecin / dentiste | Praticiens proches, langues parlées si connu | IA + P |
| Contact du propriétaire / gestionnaire | Nom, téléphone, WhatsApp, disponibilités, contact de secours (voisin, femme de ménage) | P |
| Sécurité du logement | Extincteur, trousse de premiers secours, détecteurs (emplacements), consignes d'évacuation | P |

### E. Services à la demande
| Élément | Champs | Source |
|---|---|---|
| **Taxi / VTC** | Compagnies locales (tél, appli), station la plus proche | IA |
| **Nounou / baby-sitting** | Services locaux recommandés, agences avec lien | P + IA |
| **Livraison de nourriture à domicile** | Plateformes actives dans la zone (Glovo, Uber Eats, Just Eat…), restaurants livrant en direct | IA + P |
| Ménage supplémentaire, chef à domicile, massage… | Prestataires recommandés par le propriétaire | P |
| Location (vélo, voiture, matériel de plage) | Loueurs proches, liens | IA + P |

### F. Restaurants et sorties
| Élément | Champs | Source |
|---|---|---|
| **Restaurants recommandés** | Sélection du propriétaire (ses coups de cœur, avec commentaire personnel) + suggestions IA par catégorie (gastronomique, familial, tapas, végétarien…), distance, lien, téléphone pour réserver | P + IA |
| Bars, cafés, vie nocturne | Suggestions par ambiance | IA |

### G. Activités et tourisme
| Élément | Champs | Source |
|---|---|---|
| Plages / nature | Les plus proches, accès, drapeaux/surveillance | IA + P |
| Sites touristiques | Monuments, musées, parcs — distance, horaires, **liens de réservation** | IA |
| Activités familles / enfants | Parcs aquatiques, aires de jeux, mini-golf… | IA |
| Sport et loisirs | Golf, tennis, randonnées, sports nautiques | IA |
| Événements et fêtes locales | Fêtes de village, saisons, agenda (lien office de tourisme) | IA |
| Excursions à la journée | Suggestions à moins de 1 h–1 h 30 | IA + P |

### H. Transports
| Élément | Champs | Source |
|---|---|---|
| Transports en commun | Arrêts de bus/tram/train proches, lignes utiles, liens horaires, appli locale | IA |
| Aéroports | Distance, options de transfert | IA |

### I. Informations administratives et légales
| Élément | Champs | Source |
|---|---|---|
| Taxe de séjour | Montant, modalités si applicable | P |
| Numéro d'enregistrement du logement | Licence touristique (obligatoire en Espagne : n° VT/VUT) | P |
| Conditions et caution | Rappel des conditions de location | P |
| Assurance / responsabilité | Mentions utiles | P |

---

## 5. Pipeline d'enrichissement automatique par IA

### 5.1 Déroulé
1. **Géocodage** de l'adresse → coordonnées GPS (Nominatim/OSM ; fallback Google Geocoding si ambiguïté)
2. **Recherche de POI** par catégorie et rayon adapté (ex. pharmacie 2 km, hôpital 25 km, aéroport 100 km) via **Overpass API (OpenStreetMap)** : nom, coordonnées, téléphone, horaires, site web quand disponibles
3. **Enrichissement IA (API Claude + web search)** :
   - compléter les données manquantes (liens web, horaires de marchés, plateformes de livraison actives dans la zone)
   - rédiger de courtes descriptions dans le ton du guide
   - déterminer les numéros d'urgence et consignes de tri propres au pays/à la commune
4. **Calcul des distances/temps** à pied et en voiture (OSRM auto-hébergé ou service gratuit)
5. **Constitution du brouillon** → présenté au propriétaire pour validation élément par élément
6. **Mise en cache** : tout est stocké en base ; aucun appel API lors des consultations voyageurs

### 5.2 Règles
- Aucune donnée enrichie n'est publiée sans validation du propriétaire
- Chaque POI conserve sa **source** et sa **date de récupération** ; suggestion de rafraîchissement au-delà de 6–12 mois
- Quotas d'enrichissement par formule d'abonnement (maîtrise des coûts)
- Journalisation des coûts API par logement (pilotage de la marge)

---

## 6. Carte interactive

- Bibliothèque : **Leaflet** (ou MapLibre GL) + tuiles OpenStreetMap — gratuit
- Logement au centre avec marqueur distinctif
- POI affichés par **catégories filtrables** (santé, commerces, restaurants, activités, transports) avec icônes et couleurs cohérentes
- Au clic sur un POI : fiche (nom, distance à pied et en voiture, horaires, téléphone cliquable, lien web, bouton « itinéraire » ouvrant l'app de navigation du téléphone)
- Cercles de distance optionnels (500 m / 1 km / 5 km)
- Mode hors-ligne : tuiles de la zone mises en cache à la première visite (rayon limité)

---

## 7. Architecture technique

| Couche | Choix | Justification |
|---|---|---|
| Backend | **Python / FastAPI** | Compétence existante, performance, écosystème |
| Base de données | **PostgreSQL + PostGIS** | Requêtes géographiques natives, robustesse multi-tenant |
| Frontend voyageur | **PWA** (framework JS léger — Vue ou Svelte — ou HTMX si l'on veut rester très Python) | Mobile-first, hors-ligne, installable ; empaquetage Capacitor possible en V3 pour les stores |
| Back-office | Même stack front, ou Django Admin-like si pivot Django | Rapidité de développement |
| Cartographie | Leaflet + OSM ; OSRM pour les itinéraires | Coût zéro |
| Données POI | Overpass API (OSM) ; option Google Places en V2/V3 | Coût zéro au départ, très bonne couverture européenne |
| IA | **API Claude** (+ outil web search) | Enrichissement, rédaction, traduction de secours |
| Traductions | Pré-traduction stockée par langue (Claude ou DeepL), pas de traduction à la volée | Coût maîtrisé, hors-ligne possible |
| Paiement | Stripe (abonnements) | Standard |
| Hébergement | VPS européen ou PaaS (Scaleway, OVH, Railway…) | RGPD, coût |
| Fichiers/photos | Stockage objet type S3 compatible | Scalabilité |

**Multi-tenant :** isolation logique par `owner_id` sur toutes les tables (row-level security PostgreSQL en option), un compte pouvant posséder N logements.

---

## 8. Sécurité et confidentialité (RGPD)

- **Données sensibles** (code boîte à clés, mot de passe wifi, coordonnées du propriétaire) : chiffrées en base (chiffrement applicatif), jamais indexables par les moteurs de recherche (`noindex`, lien à token ≥ 128 bits)
- Options d'accès au guide : lien secret (MVP) → code PIN → fenêtre temporelle liée au séjour (V2)
- Le guide public peut masquer les sections sensibles tant que le voyageur n'a pas saisi le code (V2)
- HTTPS partout, en-têtes de sécurité, limitation de débit sur l'API
- RGPD : hébergement UE, registre des traitements, suppression de compte et export des données, consentement pour les statistiques de consultation, DPA avec les sous-traitants (Stripe, hébergeur, Anthropic)
- Sauvegardes chiffrées quotidiennes, rétention 30 jours

---

## 9. Multilingue

- Interface propriétaire : FR + ES + EN (marché initial Espagne/France)
- Guide voyageur : contenu source dans la langue du propriétaire, **traductions générées et stockées** en EN/ES/FR/DE/NL (extensible) ; re-traduction automatique à chaque modification de section
- Le propriétaire peut relire/corriger une traduction

---

## 10. Modèle économique (proposition à affiner)

| Formule | Prix indicatif | Contenu |
|---|---|---|
| **Gratuit / Essai** | 0 € | 1 logement, sections limitées, filigrane, 1 enrichissement IA |
| **Solo** | ~6–9 €/mois par logement | Guide complet, enrichissement, multilingue, QR PDF |
| **Pro (conciergerie)** | Dégressif au volume | Multi-logements, duplication, co-gestionnaires, marque blanche (V3) |

Facturation mensuelle/annuelle via Stripe. Coût marginal par logement (API) estimé < 0,50 € grâce à la stratégie OSM + cache.

---

## 11. Exigences non fonctionnelles

- **Performance :** guide voyageur < 2 s au premier chargement en 4G ; carte fluide sur mobile d'entrée de gamme
- **Disponibilité :** cible 99,5 % (MVP)
- **Accessibilité :** contrastes suffisants, tailles de police adaptées (public senior), navigation simple
- **Compatibilité :** iOS Safari + Android Chrome des 4 dernières années
- **Scalabilité :** dimensionné pour 10 000 logements sans refonte

---

## 12. Phasage

### Phase 1 — MVP (objectif : premiers propriétaires payants)
1. Auth + gestion de logements
2. Formulaire guidé complet (toutes les sections du §4, saisie manuelle)
3. Enrichissement automatique v1 (géocodage + Overpass + Claude, catégories principales : urgences/santé, commerces, restaurants, poubelles pays)
4. Guide voyageur PWA + carte Leaflet + distances
5. QR code + lien secret
6. FR + EN + ES

### Phase 2
- Traductions étendues, accès par dates de séjour, statistiques, signalement d'infos obsolètes, duplication de guides, facturation Stripe complète, hors-ligne avancé

### Phase 3
- Apps stores (Capacitor), marque blanche conciergeries, intégrations calendriers (iCal Airbnb/Booking) pour l'accès limité au séjour, messagerie voyageur↔propriétaire, Google Places en option qualité

---

## 13. Indicateurs de succès

- Taux de complétude moyen des guides (> 80 %)
- Part des suggestions IA acceptées sans modification (> 60 %)
- Temps de création d'un guide (< 45 min)
- Consultations par séjour, taux de questions évitées (enquête propriétaires)
- Conversion essai → payant, churn mensuel

---

## 14. Risques et points ouverts

| Risque / question | Piste |
|---|---|
| Qualité inégale des données OSM dans certaines zones rurales | Fallback Google Places ciblé, contribution du propriétaire |
| Exactitude des infos IA (horaires, numéros) | Validation obligatoire du propriétaire + affichage de la date de vérification |
| Concurrence (livrets d'accueil existants) | Différenciation : enrichissement IA + carte + multilingue automatique |
| Nom du produit et nom de domaine | À choisir |
| Statut juridique / CGV / mentions légales | À prévoir avant commercialisation |
| Mode hors-ligne des tuiles carte | Limiter le rayon mis en cache pour maîtriser le poids |

---

*Document de travail — toute section peut être amendée avant le démarrage du développement.*
