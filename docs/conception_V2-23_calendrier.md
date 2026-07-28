# Conception V2-23 — Calendrier des locations & préparation par séjour
*Document de réflexion préparé le 27/07/2026 au soir — à discuter entre André et Claude (architecte) avant rédaction des briefs. Rien n'est figé. Public : André (décisions §8) + Claude (dérivation des briefs V2-23a/b pour Claude Code).*

## 1. La vision en une phrase

Chaque logement gagne un **calendrier des séjours** (importé des plateformes + saisies directes), et chaque séjour porte sa **fiche de préparation** (demandes particulières, heures, interventions calculées) — consultable par le propriétaire dans le back-office et par l'équipe d'entretien dans le cahier `/s/`, qui gagne un **planning**.

## 2. Modèle de données proposé

**Table `bookings`**
- `id`, `property_id` (FK)
- `starts_on`, `ends_on` (dates) + `checkin_time`, `checkout_time` (heures — NULL = heures standard du logement)
- `source` : `airbnb` | `vrbo` | `booking` | `direct` | `other`
- `external_uid` (l'UID iCal — clé d'idempotence des imports) + `calendar_id` (FK, NULL si saisie directe)
- `guest_name`, `guest_contact` (NULL pour les imports — à compléter à la main), `notes`
- `status` : `confirmed` | `cancelled` (un bloc disparu du flux iCal = annulé, jamais supprimé — invariant maison)
- `special_requests` JSONB (V2-23b — [{code:"crib", qty:1}, {code:"parasol"}...])

**Table `property_calendars`** (les flux du logement)
- `id`, `property_id`, `platform`, `ical_url` (⚠️ secret : l'URL iCal donne accès au calendrier — chiffrer AES comme les wifi), `last_sync_at`, `last_sync_status`, `sync_error`

**Sur `properties`** : `default_checkin_time` / `default_checkout_time` (les heures standard, ex. 15:00/10:00)

**Table `care_rules`** (V2-23b — règles d'entretien par logement)
- `property_id`, `rule` : ex. `linen_change` {included: true, from_day: 8}, `midstay_cleaning` {included: false, on_request: true} — extensible en JSONB plutôt qu'en colonnes figées

**Table `special_request_catalog`** (par logement, avec un catalogue par défaut seedé : lit bébé, chaise haute, parasol, ménage supplémentaire, draps supplémentaires...) — libellés localisés.

## 3. L'import iCal — mécanique et pièges connus d'avance

- **Le propriétaire colle l'URL iCal** de son annonce (chaque plateforme l'expose : Airbnb « Exporter le calendrier », Vrbo/Abritel idem, Booking aussi). On valide le flux au collage (fetch + parse immédiat, feedback direct).
- **Rafraîchissement** : timer systemd (patron connu — backups, relances) toutes les **3-4 h** ; les plateformes elles-mêmes ne synchronisent pas plus vite. Bouton « Synchroniser maintenant » pour l'impatience légitime.
- **Parsing** : les VEVENT donnent DTSTART/DTEND (souvent en dates entières), un SUMMARY pauvre (« Reserved », « Blocked »), un UID stable → **upsert par UID** (idempotent, notre marque de fabrique). Les événements « Blocked/Not available » (fermetures manuelles côté plateforme) : à importer comme `status='blocked'` distinct d'une réservation ? → à trancher (penchant pour oui, discret).
- **Ce que le flux ne donne PAS** : nom complet, contact, demandes. L'UX assume : le bloc importé s'affiche « Airbnb — séjour du X au Y » avec un bouton « Compléter » (nom, heures, demandes). Une saisie de 30 secondes, une fois par séjour.
- **Disparition d'un événement du flux** = annulation → `status='cancelled'` (conservé, grisé), jamais de suppression silencieuse.
- **Dépendance** : parser iCal en Python — `icalendar` (mûr, sans dépendances lourdes) plutôt qu'un parsing artisanal. Réseau sortant : les fetches iCal sont des appels serveur→plateformes (nouveau flux sortant à documenter dans CLAUDE.md ; timeouts courts, User-Agent propre, jamais bloquant pour le reste).

## 4. Anti-chevauchement

Détection à chaque import/saisie : deux bookings `confirmed` du même logement qui se recouvrent → **alerte visible** (bandeau rouge sur le calendrier + pastille sur la carte du logement), jamais de blocage automatique (le propriétaire arbitre — c'est peut-être un chevauchement volontaire de 2 h le jour de rotation, les dates iCal étant entières). Le cas « même jour départ/arrivée » n'est PAS un chevauchement : c'est une **rotation**, affichée comme telle avec sa fenêtre (checkout 10h → checkin 15h = 5 h de préparation).

## 5. Les vues

**Back-office propriétaire** (nouvel onglet « Séjours » sur le logement, ou vue dédiée depuis la carte) :
- Liste chronologique (plus lisible qu'une grille mensuelle sur mobile, et plus simple à livrer en V1) : « 12.08 → 31.08 · Dupont · [Abritel] · ✓ complété » — badges couleur par plateforme.
- Fiche séjour : dates, heures (standard ou ajustées), nom/contact, demandes particulières (cases du catalogue), notes, interventions calculées (« Changement de draps prévu vers le 19.08 »).
- Une vraie grille calendrier peut venir en V2 si le besoin se confirme.

**Cahier staff `/s/`** (l'atout maître) :
- Nouveau bloc « Planning » en tête : les 4-6 prochaines semaines — arrivées ▶, départs ◀, **rotations même jour mises en évidence** avec la fenêtre horaire, et par séjour : les demandes à préparer + interventions (draps J+8...).
- Le staff ne voit que l'opérationnel : dates, heures, prénom, demandes — pas les notes privées ni les contacts complets (à trancher : niveau de détail exposé au staff).

## 6. Répartition par plan (proposition à valider)

| | Essai | Solo | Pro |
|---|---|---|---|
| Calendrier + saisie directe + anti-chevauchement | ✓ | ✓ | ✓ |
| Import iCal automatique | ✓ | ✓ (1 flux/logement ?) | ✓ (multi-flux) |
| Fiches de préparation + demandes particulières | ✓ (aperçu) | ✓ | ✓ |
| **Planning staff dans `/s/` + règles d'entretien** | aperçu | **✗** | **✓** |

Logique : le calendrier nourrit la *rétention* de tous ; le planning staff est le prolongement naturel de l'exclusivité Pro du cahier (V2-18b) et **la** feature qui fait basculer les conciergeries. Cohérent avec « la limite est une vitrine » (V2-22) : le Solo voit le bloc planning verrouillé avec badge Pro.

## 7. Découpage en missions (proposition)

1. **V2-23a — Le calendrier** (~1 session) : tables bookings/calendars + heures standard, import iCal (parser, upsert par UID, timer, bouton sync, URL chiffrée), saisie directe, anti-chevauchement + rotations, vue « Séjours » back-office. Validation avec le **flux réel de Villa Ballarin (Vrbo n° 1281695)**.
2. **V2-23b — La préparation** (~1 session) : catalogue de demandes + fiche séjour complète, heures ajustées par séjour, règles d'entretien + interventions calculées, **planning staff** dans `/s/` avec gating Pro, badges V2-22 au passage sur ce bloc.
3. (plus tard) V2-23c éventuel : grille mensuelle, export du planning, notifications staff.

## 8. Questions ouvertes pour André (les réponses calibreront les briefs)

1. **Statuts des blocs « Blocked »** des plateformes : les afficher (grisés) ou les ignorer ?
2. **Niveau de détail exposé au staff** : prénom seul ? nom complet ? jamais le téléphone du locataire ?
3. **Multi-flux par logement** dès la V1 (un logement sur Airbnb ET Abritel) ou un seul flux pour commencer ? (recommandation : multi d'emblée — le modèle le permet sans surcoût)
4. Le **catalogue de demandes par défaut** : la liste de terrain (lit bébé, chaise haute, parasol, quoi d'autre revient souvent ?)
5. La règle draps/ménage (draps inclus dès le 8ᵉ jour ; ménage en cours de séjour non inclus) est-elle **universelle** (défaut pour tous les logements) ou spécifique à Ballarin ? → le modèle prévoit par-logement avec un défaut seedé, mais le défaut doit être le bon.
