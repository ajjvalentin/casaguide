-- ============================================================================
-- CasaGuide — Seed des données de référence (v1.0)
-- Contenu : plans d’abonnement (§10), catégories de POI (§4/§6),
--           catalogue des sections pré-définies — la checklist du §4 du CdC
-- Idempotent : ré-exécutable à volonté (ON CONFLICT ... DO UPDATE)
-- ============================================================================

BEGIN;

-- ============================================================================
-- 1. PLANS D’ABONNEMENT (§10 — montants indicatifs, à affiner)
-- ============================================================================

INSERT INTO plans (id, name, max_properties, enrich_quota, price_month_cts, features) VALUES
('free', 'Essai gratuit', 1,    1,  0,    '{"langs": 1, "watermark": true,  "stats": false, "white_label": false}'),
('solo', 'Solo',          3,    3,  790,  '{"langs": 5, "watermark": false, "stats": true,  "white_label": false}'),
('pro',  'Pro',           NULL, 10, 2900, '{"langs": 5, "watermark": false, "stats": true,  "white_label": true}')
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name, max_properties = EXCLUDED.max_properties,
  enrich_quota = EXCLUDED.enrich_quota, price_month_cts = EXCLUDED.price_month_cts,
  features = EXCLUDED.features;

-- ============================================================================
-- 2. CATÉGORIES DE POI (§4, §6)
--    default_radius_m = rayon de recherche Overpass adapté à chaque catégorie
--    icon = nom d’icône Lucide ; map_color = couleur du chapitre sur la carte
-- ============================================================================

INSERT INTO poi_categories (code, chapter, name_i18n, icon, map_color, default_radius_m) VALUES
-- A — Arrivée
('parking',        'A', '{"fr":"Parking","en":"Parking","es":"Aparcamiento"}',                              'circle-parking',  '#546E7A', 1000),
-- C — Vie pratique
('supermarket',    'C', '{"fr":"Supermarché","en":"Supermarket","es":"Supermercado"}',                      'shopping-cart',   '#2E7D32', 3000),
('market',         'C', '{"fr":"Marché local","en":"Local market","es":"Mercado local"}',                   'store',           '#2E7D32', 8000),
('bakery',         'C', '{"fr":"Boulangerie","en":"Bakery","es":"Panadería"}',                              'croissant',       '#2E7D32', 2000),
('atm',            'C', '{"fr":"Distributeur","en":"ATM","es":"Cajero"}',                                   'banknote',        '#2E7D32', 2000),
('post_office',    'C', '{"fr":"Poste","en":"Post office","es":"Correos"}',                                 'mail',            '#2E7D32', 5000),
('mall',           'C', '{"fr":"Centre commercial","en":"Shopping mall","es":"Centro comercial"}',          'shopping-bag',    '#2E7D32', 15000),
('laundry',        'C', '{"fr":"Laverie","en":"Laundry","es":"Lavandería"}',                                'shirt',           '#2E7D32', 5000),
-- D — Urgences & santé
('hospital',       'D', '{"fr":"Hôpital","en":"Hospital","es":"Hospital"}',                                 'cross',           '#C62828', 25000),
('pharmacy',       'D', '{"fr":"Pharmacie","en":"Pharmacy","es":"Farmacia"}',                               'pill',            '#C62828', 3000),
('doctor',         'D', '{"fr":"Médecin / dentiste","en":"Doctor / dentist","es":"Médico / dentista"}',     'stethoscope',     '#C62828', 5000),
('police',         'D', '{"fr":"Police","en":"Police","es":"Policía"}',                                     'shield',          '#C62828', 10000),
('veterinary',     'D', '{"fr":"Vétérinaire","en":"Veterinary","es":"Veterinario"}',                        'paw-print',       '#C62828', 10000),
-- E — Services
('taxi',           'E', '{"fr":"Taxi / VTC","en":"Taxi / ride-hailing","es":"Taxi / VTC"}',                 'car-taxi-front',  '#6A1B9A', 10000),
('babysitter',     'E', '{"fr":"Baby-sitting","en":"Babysitting","es":"Canguro"}',                          'baby',            '#6A1B9A', 15000),
('food_delivery',  'E', '{"fr":"Livraison de repas","en":"Food delivery","es":"Comida a domicilio"}',       'bike',            '#6A1B9A', 10000),
('rental',         'E', '{"fr":"Location (vélo, voiture…)","en":"Rentals (bike, car…)","es":"Alquiler (bici, coche…)"}', 'key-round', '#6A1B9A', 10000),
-- F — Restaurants & sorties
('restaurant',     'F', '{"fr":"Restaurant","en":"Restaurant","es":"Restaurante"}',                         'utensils',        '#EF6C00', 3000),
('bar',            'F', '{"fr":"Bar","en":"Bar","es":"Bar"}',                                               'martini',         '#EF6C00', 3000),
('cafe',           'F', '{"fr":"Café","en":"Café","es":"Cafetería"}',                                       'coffee',          '#EF6C00', 2000),
-- G — Activités & tourisme
('beach',          'G', '{"fr":"Plage","en":"Beach","es":"Playa"}',                                         'waves',           '#0277BD', 10000),
('sight',          'G', '{"fr":"Site touristique","en":"Sight","es":"Lugar de interés"}',                   'landmark',        '#0277BD', 20000),
('family_activity','G', '{"fr":"Activité famille","en":"Family activity","es":"Actividad familiar"}',       'ferris-wheel',    '#0277BD', 15000),
('sport',          'G', '{"fr":"Sport & loisirs","en":"Sports & leisure","es":"Deporte y ocio"}',           'dumbbell',        '#0277BD', 10000),
-- H — Transports
('bus_stop',       'H', '{"fr":"Arrêt de bus","en":"Bus stop","es":"Parada de bus"}',                       'bus',             '#00695C', 1000),
('train_station',  'H', '{"fr":"Gare","en":"Train station","es":"Estación de tren"}',                       'train-front',     '#00695C', 15000),
('airport',        'H', '{"fr":"Aéroport","en":"Airport","es":"Aeropuerto"}',                               'plane',           '#00695C', 100000)
ON CONFLICT (code) DO UPDATE SET
  chapter = EXCLUDED.chapter, name_i18n = EXCLUDED.name_i18n, icon = EXCLUDED.icon,
  map_color = EXCLUDED.map_color, default_radius_m = EXCLUDED.default_radius_m;

-- ============================================================================
-- 3. CATALOGUE DES SECTIONS — la checklist complète du §4
--    field_schema :
--      "fields"         : champs structurés du formulaire propriétaire
--                         (types : text, textarea, time, bool, number, phone, url, select)
--      "repeat"         : groupe de champs répétable (ex. liste d’équipements)
--      "poi_categories" : catégories de POI rattachées à la section (carte + liste)
--      "area_facts"     : types de données pays/commune injectées par l’IA
--    ai_enrichable = pré-remplissable par le pipeline (§5), toujours validé
--    is_sensitive  = masquée aux visiteurs non authentifiés (V2, §8)
-- ============================================================================

INSERT INTO section_templates
  (code, chapter, sort_order, icon, name_i18n, description_i18n, field_schema, ai_enrichable, is_sensitive)
VALUES

-- ─── A. ARRIVÉE ET DÉPART ───────────────────────────────────────────────────
('A_checkin', 'A', 10, 'door-open',
 '{"fr":"Check-in","en":"Check-in","es":"Llegada"}',
 '{"fr":"Heure d’arrivée au plus tôt et déroulé de l’arrivée (autonome ou accueil en personne)."}',
 '{"fields":[
    {"key":"checkin_from","type":"time","label":{"fr":"Arrivée à partir de","en":"Check-in from","es":"Llegada a partir de"}},
    {"key":"self_checkin","type":"bool","label":{"fr":"Arrivée autonome","en":"Self check-in","es":"Llegada autónoma"}},
    {"key":"procedure","type":"textarea","label":{"fr":"Déroulé de l’arrivée","en":"Arrival procedure","es":"Procedimiento de llegada"}}
  ]}', FALSE, FALSE),

('A_checkout', 'A', 20, 'log-out',
 '{"fr":"Check-out","en":"Check-out","es":"Salida"}',
 '{"fr":"Heure limite de départ et consignes : clés, poubelles, vaisselle, linge, fenêtres…"}',
 '{"fields":[
    {"key":"checkout_until","type":"time","label":{"fr":"Départ avant","en":"Check-out before","es":"Salida antes de"}},
    {"key":"tasks","type":"textarea","label":{"fr":"Consignes de départ","en":"Departure tasks","es":"Tareas de salida"}}
  ]}', FALSE, FALSE),

('A_keybox', 'A', 30, 'lock-keyhole',
 '{"fr":"Boîte à clés & accès aux clés","en":"Key box & key access","es":"Caja de llaves y acceso"}',
 '{"fr":"Emplacement de la boîte à clés (photo recommandée) et instructions. Le code lui-même est saisi dans l’espace sécurisé et chiffré."}',
 '{"fields":[
    {"key":"location","type":"textarea","label":{"fr":"Emplacement et instructions","en":"Location and instructions","es":"Ubicación e instrucciones"}}
  ],"secrets":["keybox_code"]}', FALSE, TRUE),

('A_access', 'A', 40, 'map-pin',
 '{"fr":"Accès au logement","en":"Getting to the property","es":"Acceso al alojamiento"}',
 '{"fr":"Étage, interphone, portail, digicode d’immeuble, repères visuels (photo de la façade recommandée)."}',
 '{"fields":[
    {"key":"floor","type":"text","label":{"fr":"Étage / porte","en":"Floor / door","es":"Piso / puerta"}},
    {"key":"intercom","type":"text","label":{"fr":"Interphone","en":"Intercom","es":"Portero automático"}},
    {"key":"details","type":"textarea","label":{"fr":"Portail, digicode, repères…","en":"Gate, entry code, landmarks…","es":"Portal, código, referencias…"}}
  ]}', FALSE, FALSE),

('A_parking', 'A', 50, 'circle-parking',
 '{"fr":"Parking","en":"Parking","es":"Aparcamiento"}',
 '{"fr":"Place privée, stationnement dans la rue ou parking public le plus proche (tarifs, lien)."}',
 '{"fields":[
    {"key":"parking_type","type":"select","options":["private","street","public"],"label":{"fr":"Type de stationnement","en":"Parking type","es":"Tipo de aparcamiento"}},
    {"key":"details","type":"textarea","label":{"fr":"Détails","en":"Details","es":"Detalles"}}
  ],"poi_categories":["parking"]}', TRUE, FALSE),

('A_arrival', 'A', 60, 'plane-landing',
 '{"fr":"Venir depuis l’aéroport / la gare","en":"From the airport / station","es":"Desde el aeropuerto / la estación"}',
 '{"fr":"Itinéraires recommandés, navettes, ordre de prix d’un taxi."}',
 '{"fields":[
    {"key":"from_airport","type":"textarea","label":{"fr":"Depuis l’aéroport","en":"From the airport","es":"Desde el aeropuerto"}},
    {"key":"from_station","type":"textarea","label":{"fr":"Depuis la gare","en":"From the station","es":"Desde la estación"}}
  ],"poi_categories":["airport","train_station"]}', TRUE, FALSE),

-- ─── B. LE LOGEMENT ─────────────────────────────────────────────────────────
('B_wifi', 'B', 110, 'wifi',
 '{"fr":"Wifi","en":"Wifi","es":"Wifi"}',
 '{"fr":"Nom du réseau et emplacement de la box. Le mot de passe est saisi dans l’espace sécurisé et chiffré ; un QR de connexion automatique sera généré."}',
 '{"fields":[
    {"key":"router_location","type":"text","label":{"fr":"Emplacement de la box","en":"Router location","es":"Ubicación del router"}},
    {"key":"notes","type":"textarea","label":{"fr":"Remarques (redémarrage, débit…)","en":"Notes (restart, speed…)","es":"Notas (reinicio, velocidad…)"}}
  ],"secrets":["wifi_pass"]}', FALSE, TRUE),

('B_appliances', 'B', 120, 'washing-machine',
 '{"fr":"Équipements intérieurs","en":"Indoor appliances","es":"Equipamiento interior"}',
 '{"fr":"Climatisation, chauffage, lave-linge, lave-vaisselle, TV, four, cafetière… Une fiche par équipement, avec photos ou notice PDF."}',
 '{"repeat":{"key":"appliances","fields":[
    {"key":"name","type":"text","label":{"fr":"Équipement","en":"Appliance","es":"Equipo"}},
    {"key":"instructions","type":"textarea","label":{"fr":"Mode d’emploi","en":"Instructions","es":"Instrucciones"}}
  ]}}', FALSE, FALSE),

('B_appliances_out', 'B', 125, 'sun',
 '{"fr":"Équipements extérieurs","en":"Outdoor equipment","es":"Equipamiento exterior"}',
 '{"fr":"Plancha, barbecue, stores électriques, éclairage et équipements de la piscine, arrosage, mobilier de jardin… Une fiche par équipement, avec photos (télécommandes, boutons…)."}',
 '{"repeat":{"key":"appliances","fields":[
    {"key":"name","type":"text","label":{"fr":"Équipement","en":"Equipment","es":"Equipo"}},
    {"key":"instructions","type":"textarea","label":{"fr":"Mode d’emploi","en":"Instructions","es":"Instrucciones"}}
  ]}}', FALSE, FALSE),

('B_pool', 'B', 130, 'waves-ladder',
 '{"fr":"Piscine / jacuzzi / barbecue","en":"Pool / hot tub / BBQ","es":"Piscina / jacuzzi / barbacoa"}',
 '{"fr":"Règles d’usage, horaires, sécurité enfants, entretien. Masquez cette section si non concerné."}',
 '{"fields":[
    {"key":"rules","type":"textarea","label":{"fr":"Règles et consignes","en":"Rules and guidelines","es":"Normas e indicaciones"}}
  ]}', FALSE, FALSE),

('B_utilities', 'B', 140, 'plug-zap',
 '{"fr":"Compteurs & disjoncteurs","en":"Meters & breakers","es":"Contadores y cuadro eléctrico"}',
 '{"fr":"Emplacements eau / électricité / gaz et que faire en cas de coupure."}',
 '{"fields":[
    {"key":"electricity","type":"textarea","label":{"fr":"Électricité (tableau, disjoncteur)","en":"Electricity (panel, breaker)","es":"Electricidad (cuadro, diferencial)"}},
    {"key":"water","type":"textarea","label":{"fr":"Eau (arrivée, chauffe-eau)","en":"Water (main valve, heater)","es":"Agua (llave, calentador)"}},
    {"key":"gas","type":"textarea","label":{"fr":"Gaz (le cas échéant)","en":"Gas (if any)","es":"Gas (si procede)"}}
  ]}', FALSE, FALSE),

('B_house_rules', 'B', 150, 'scroll-text',
 '{"fr":"Règlement intérieur","en":"House rules","es":"Normas de la casa"}',
 '{"fr":"Fumeurs, animaux, fêtes, nombre maximum d’occupants, heures de silence. Les règles locales sur le bruit sont pré-remplies automatiquement."}',
 '{"fields":[
    {"key":"smoking","type":"bool","label":{"fr":"Fumeurs autorisés","en":"Smoking allowed","es":"Se permite fumar"}},
    {"key":"pets","type":"bool","label":{"fr":"Animaux autorisés","en":"Pets allowed","es":"Se admiten mascotas"}},
    {"key":"parties","type":"bool","label":{"fr":"Fêtes autorisées","en":"Parties allowed","es":"Se permiten fiestas"}},
    {"key":"max_guests","type":"number","label":{"fr":"Occupants maximum","en":"Max guests","es":"Ocupantes máximos"}},
    {"key":"quiet_hours","type":"text","label":{"fr":"Heures de silence","en":"Quiet hours","es":"Horas de silencio"}},
    {"key":"other","type":"textarea","label":{"fr":"Autres règles","en":"Other rules","es":"Otras normas"}}
  ],"area_facts":["noise_rules"]}', TRUE, FALSE),

('B_cleaning', 'B', 160, 'brush-cleaning',
 '{"fr":"Ménage & linge","en":"Cleaning & linen","es":"Limpieza y ropa de casa"}',
 '{"fr":"Où trouver draps, serviettes et produits ; consignes de ménage."}',
 '{"fields":[
    {"key":"linen","type":"textarea","label":{"fr":"Draps et serviettes","en":"Sheets and towels","es":"Sábanas y toallas"}},
    {"key":"supplies","type":"textarea","label":{"fr":"Produits fournis","en":"Supplies provided","es":"Productos disponibles"}}
  ]}', FALSE, FALSE),

-- ─── C. VIE PRATIQUE ────────────────────────────────────────────────────────
('C_trash', 'C', 210, 'trash-2',
 '{"fr":"Poubelles & tri","en":"Trash & recycling","es":"Basura y reciclaje"}',
 '{"fr":"Emplacement des conteneurs (photo + point sur la carte), jours de collecte, consignes de tri locales (pré-remplies automatiquement)."}',
 '{"fields":[
    {"key":"container_location","type":"textarea","label":{"fr":"Emplacement des conteneurs","en":"Container location","es":"Ubicación de los contenedores"}},
    {"key":"schedule","type":"textarea","label":{"fr":"Jours / horaires de collecte","en":"Collection days / times","es":"Días / horarios de recogida"}}
  ],"area_facts":["waste_rules"]}', TRUE, FALSE),

('C_supermarkets', 'C', 220, 'shopping-cart',
 '{"fr":"Supermarchés & commerces","en":"Supermarkets & shops","es":"Supermercados y comercios"}',
 '{"fr":"Les commerces les plus proches, avec horaires et distance — suggérés automatiquement, à valider."}',
 '{"poi_categories":["supermarket","bakery"]}', TRUE, FALSE),

('C_markets', 'C', 230, 'store',
 '{"fr":"Marchés locaux","en":"Local markets","es":"Mercados locales"}',
 '{"fr":"Jours, horaires, emplacement et spécialités des marchés hebdomadaires alentour."}',
 '{"poi_categories":["market"]}', TRUE, FALSE),

('C_shops', 'C', 240, 'banknote',
 '{"fr":"Services de proximité","en":"Nearby services","es":"Servicios cercanos"}',
 '{"fr":"Distributeur de billets, bureau de poste, tabac…"}',
 '{"poi_categories":["atm","post_office"]}', TRUE, FALSE),

('C_malls', 'C', 250, 'shopping-bag',
 '{"fr":"Centres commerciaux","en":"Shopping malls","es":"Centros comerciales"}',
 '{"fr":"Centres commerciaux accessibles, avec horaires et lien vers leur site."}',
 '{"poi_categories":["mall"]}', TRUE, FALSE),

('C_laundry', 'C', 260, 'shirt',
 '{"fr":"Laverie","en":"Laundry","es":"Lavandería"}',
 '{"fr":"Laveries les plus proches — utile si le logement n’a pas de lave-linge."}',
 '{"poi_categories":["laundry"]}', TRUE, FALSE),

-- ─── D. URGENCES ET SANTÉ ───────────────────────────────────────────────────
('D_emergency', 'D', 310, 'siren',
 '{"fr":"Numéros d’urgence","en":"Emergency numbers","es":"Números de emergencia"}',
 '{"fr":"112, urgences médicales, pompiers, police locale… pré-remplis selon le pays et la commune, à vérifier."}',
 '{"area_facts":["emergency_numbers"],"poi_categories":["police"]}', TRUE, FALSE),

('D_hospital', 'D', 320, 'cross',
 '{"fr":"Hôpital le plus proche","en":"Nearest hospital","es":"Hospital más cercano"}',
 '{"fr":"Hôpital et urgences 24 h les plus proches, avec adresse, téléphone et itinéraire."}',
 '{"poi_categories":["hospital"]}', TRUE, FALSE),

('D_pharmacy', 'D', 330, 'pill',
 '{"fr":"Pharmacies","en":"Pharmacies","es":"Farmacias"}',
 '{"fr":"Pharmacies proches et lien vers le planning des pharmacies de garde."}',
 '{"fields":[
    {"key":"on_duty_url","type":"url","label":{"fr":"Lien pharmacies de garde","en":"On-duty pharmacies link","es":"Enlace farmacias de guardia"}}
  ],"poi_categories":["pharmacy"]}', TRUE, FALSE),

('D_doctors', 'D', 340, 'stethoscope',
 '{"fr":"Médecins & vétérinaire","en":"Doctors & veterinary","es":"Médicos y veterinario"}',
 '{"fr":"Médecins, dentistes et vétérinaires proches ; précisez les langues parlées si vous les connaissez."}',
 '{"poi_categories":["doctor","veterinary"]}', TRUE, FALSE),

('D_contact', 'D', 350, 'phone-call',
 '{"fr":"Nous contacter","en":"Contact us","es":"Contacto"}',
 '{"fr":"Vos coordonnées (téléphone, WhatsApp, email), vos disponibilités et un contact de secours (voisin, femme de ménage…). Renseignées dans la fiche du logement."}',
 '{"fields":[
    {"key":"availability","type":"text","label":{"fr":"Disponibilités","en":"Availability","es":"Disponibilidad"}},
    {"key":"notes","type":"textarea","label":{"fr":"Précisions","en":"Notes","es":"Notas"}}
  ],"uses_property_contact":true}', FALSE, FALSE),

('D_safety', 'D', 360, 'shield-alert',
 '{"fr":"Sécurité du logement","en":"Home safety","es":"Seguridad del alojamiento"}',
 '{"fr":"Extincteur, trousse de premiers secours, détecteurs de fumée, consignes d’évacuation."}',
 '{"fields":[
    {"key":"extinguisher","type":"text","label":{"fr":"Extincteur (emplacement)","en":"Fire extinguisher (location)","es":"Extintor (ubicación)"}},
    {"key":"first_aid","type":"text","label":{"fr":"Trousse de secours (emplacement)","en":"First-aid kit (location)","es":"Botiquín (ubicación)"}},
    {"key":"evacuation","type":"textarea","label":{"fr":"Consignes d’évacuation","en":"Evacuation instructions","es":"Instrucciones de evacuación"}}
  ]}', FALSE, FALSE),

-- ─── E. SERVICES À LA DEMANDE ───────────────────────────────────────────────
('E_taxi', 'E', 410, 'car-taxi-front',
 '{"fr":"Taxi / VTC","en":"Taxi / ride-hailing","es":"Taxi / VTC"}',
 '{"fr":"Compagnies locales (téléphone, application), station de taxis la plus proche."}',
 '{"poi_categories":["taxi"]}', TRUE, FALSE),

('E_babysitter', 'E', 420, 'baby',
 '{"fr":"Baby-sitting","en":"Babysitting","es":"Canguro"}',
 '{"fr":"Services de garde d’enfants recommandés : agences locales ou personnes de confiance."}',
 '{"poi_categories":["babysitter"]}', TRUE, FALSE),

('E_food_delivery', 'E', 430, 'bike',
 '{"fr":"Livraison de repas","en":"Food delivery","es":"Comida a domicilio"}',
 '{"fr":"Plateformes actives dans la zone (Glovo, Uber Eats, Just Eat…) et restaurants livrant en direct."}',
 '{"fields":[
    {"key":"platforms","type":"textarea","label":{"fr":"Plateformes disponibles","en":"Available platforms","es":"Plataformas disponibles"}}
  ],"poi_categories":["food_delivery"]}', TRUE, FALSE),

('E_services', 'E', 440, 'concierge-bell',
 '{"fr":"Services supplémentaires","en":"Extra services","es":"Servicios adicionales"}',
 '{"fr":"Ménage supplémentaire, chef à domicile, massage… vos prestataires recommandés."}',
 '{"repeat":{"key":"services","fields":[
    {"key":"name","type":"text","label":{"fr":"Service","en":"Service","es":"Servicio"}},
    {"key":"contact","type":"text","label":{"fr":"Contact","en":"Contact","es":"Contacto"}}
  ]}}', FALSE, FALSE),

('E_rentals', 'E', 450, 'key-round',
 '{"fr":"Locations (vélo, voiture, plage)","en":"Rentals (bike, car, beach gear)","es":"Alquileres (bici, coche, playa)"}',
 '{"fr":"Loueurs proches : vélos, voitures, matériel de plage…"}',
 '{"poi_categories":["rental"]}', TRUE, FALSE),

-- ─── F. RESTAURANTS ET SORTIES ──────────────────────────────────────────────
('F_restaurants', 'F', 510, 'utensils',
 '{"fr":"Restaurants recommandés","en":"Recommended restaurants","es":"Restaurantes recomendados"}',
 '{"fr":"Vos coups de cœur (ajoutez un commentaire personnel, c’est ce que les voyageurs préfèrent) complétés par des suggestions automatiques par catégorie."}',
 '{"poi_categories":["restaurant"]}', TRUE, FALSE),

('F_bars', 'F', 520, 'martini',
 '{"fr":"Bars & cafés","en":"Bars & cafés","es":"Bares y cafeterías"}',
 '{"fr":"Bars, cafés et vie nocturne selon l’ambiance recherchée."}',
 '{"poi_categories":["bar","cafe"]}', TRUE, FALSE),

-- ─── G. ACTIVITÉS ET TOURISME ───────────────────────────────────────────────
('G_beaches', 'G', 610, 'waves',
 '{"fr":"Plages & nature","en":"Beaches & nature","es":"Playas y naturaleza"}',
 '{"fr":"Plages les plus proches, accès, surveillance ; espaces naturels."}',
 '{"poi_categories":["beach"]}', TRUE, FALSE),

('G_sights', 'G', 620, 'landmark',
 '{"fr":"Sites touristiques","en":"Sights","es":"Lugares de interés"}',
 '{"fr":"Monuments, musées, parcs — horaires et liens de réservation."}',
 '{"poi_categories":["sight"]}', TRUE, FALSE),

('G_family', 'G', 630, 'ferris-wheel',
 '{"fr":"Activités familles & enfants","en":"Family & kids activities","es":"Actividades familiares"}',
 '{"fr":"Parcs aquatiques, aires de jeux, mini-golf, activités par temps de pluie…"}',
 '{"poi_categories":["family_activity"]}', TRUE, FALSE),

('G_sport', 'G', 640, 'dumbbell',
 '{"fr":"Sport & loisirs","en":"Sports & leisure","es":"Deporte y ocio"}',
 '{"fr":"Golf, tennis, randonnées, sports nautiques à proximité."}',
 '{"poi_categories":["sport"]}', TRUE, FALSE),

('G_events', 'G', 650, 'party-popper',
 '{"fr":"Événements & fêtes locales","en":"Events & local festivals","es":"Eventos y fiestas locales"}',
 '{"fr":"Fêtes de village, saisons, agenda — avec lien vers l’office de tourisme."}',
 '{"fields":[
    {"key":"tourism_office_url","type":"url","label":{"fr":"Site de l’office de tourisme","en":"Tourism office website","es":"Web de la oficina de turismo"}},
    {"key":"events","type":"textarea","label":{"fr":"Événements notables","en":"Notable events","es":"Eventos destacados"}}
  ]}', TRUE, FALSE),

('G_daytrips', 'G', 660, 'mountain-snow',
 '{"fr":"Excursions à la journée","en":"Day trips","es":"Excursiones de un día"}',
 '{"fr":"Idées de sorties à moins de 1 h 30 : villes, villages, sites naturels."}',
 '{"fields":[
    {"key":"ideas","type":"textarea","label":{"fr":"Suggestions","en":"Suggestions","es":"Sugerencias"}}
  ]}', TRUE, FALSE),

-- ─── H. TRANSPORTS ──────────────────────────────────────────────────────────
('H_transit', 'H', 710, 'bus',
 '{"fr":"Transports en commun","en":"Public transport","es":"Transporte público"}',
 '{"fr":"Arrêts proches, lignes utiles, liens horaires, application locale."}',
 '{"fields":[
    {"key":"lines","type":"textarea","label":{"fr":"Lignes utiles","en":"Useful lines","es":"Líneas útiles"}},
    {"key":"app_or_url","type":"text","label":{"fr":"Application / site horaires","en":"App / timetable website","es":"App / web de horarios"}}
  ],"poi_categories":["bus_stop","train_station"]}', TRUE, FALSE),

('H_airport', 'H', 720, 'plane',
 '{"fr":"Aéroports","en":"Airports","es":"Aeropuertos"}',
 '{"fr":"Aéroports accessibles et options de transfert."}',
 '{"poi_categories":["airport"]}', TRUE, FALSE),

-- ─── I. INFORMATIONS ADMINISTRATIVES ────────────────────────────────────────
('I_tax', 'I', 810, 'receipt-euro',
 '{"fr":"Taxe de séjour","en":"Tourist tax","es":"Tasa turística"}',
 '{"fr":"Montant et modalités de la taxe de séjour, si applicable."}',
 '{"fields":[
    {"key":"amount","type":"text","label":{"fr":"Montant","en":"Amount","es":"Importe"}},
    {"key":"details","type":"textarea","label":{"fr":"Modalités","en":"Details","es":"Modalidades"}}
  ]}', FALSE, FALSE),

('I_license', 'I', 820, 'file-badge',
 '{"fr":"Enregistrement du logement","en":"Property registration","es":"Registro del alojamiento"}',
 '{"fr":"Numéro de licence touristique (obligatoire en Espagne : VT/VUT), affiché dans le guide. Renseigné dans la fiche du logement."}',
 '{"uses_property_license":true}', FALSE, FALSE),

('I_terms', 'I', 830, 'file-text',
 '{"fr":"Conditions & caution","en":"Terms & deposit","es":"Condiciones y fianza"}',
 '{"fr":"Rappel des conditions de location et de la caution."}',
 '{"fields":[
    {"key":"terms","type":"textarea","label":{"fr":"Conditions","en":"Terms","es":"Condiciones"}}
  ]}', FALSE, FALSE),

('I_insurance', 'I', 840, 'umbrella',
 '{"fr":"Assurance & responsabilité","en":"Insurance & liability","es":"Seguro y responsabilidad"}',
 '{"fr":"Mentions utiles concernant l’assurance et la responsabilité des occupants."}',
 '{"fields":[
    {"key":"notes","type":"textarea","label":{"fr":"Mentions","en":"Notes","es":"Menciones"}}
  ]}', FALSE, FALSE)

ON CONFLICT (code) DO UPDATE SET
  chapter = EXCLUDED.chapter, sort_order = EXCLUDED.sort_order, icon = EXCLUDED.icon,
  name_i18n = EXCLUDED.name_i18n, description_i18n = EXCLUDED.description_i18n,
  field_schema = EXCLUDED.field_schema, ai_enrichable = EXCLUDED.ai_enrichable,
  is_sensitive = EXCLUDED.is_sensitive;

COMMIT;

-- Vérification rapide :
-- SELECT chapter, count(*) FROM section_templates GROUP BY chapter ORDER BY chapter;
-- SELECT chapter, count(*) FROM poi_categories  GROUP BY chapter ORDER BY chapter;
