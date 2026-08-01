-- Migration 018 — Séparer téléphone et email du locataire (V2-23b, volet 3, §3.0)
--
-- Le champ unique `guest_contact` (« téléphone / email ») est un raccourci qui
-- coûte cher dès qu'on veut S'EN SERVIR : le téléphone est une ACTION (lien
-- `tel:`/WhatsApp cliquable depuis le mobile de la personne qui fait le ménage,
-- pour caler un rendez-vous en cours de séjour) ; l'email en est une autre
-- (lien `mailto:`, envoi du lien du guide). Un champ fourre-tout ne peut devenir
-- ni un bouton d'appel ni un bouton d'email, et ne se valide comme aucun des deux.
--
--   bookings.guest_phone — téléphone, forme internationale encouragée (+33…) :
--     la clientèle est FR/BE/NL/DE/CH, l'équipe compose depuis l'Espagne ; un
--     « 06 12 34 56 78 » saisi tel quel n'est pas joignable. Conservé tel quel.
--   bookings.guest_email — email du locataire (lien du guide, mot de bienvenue).
--   bookings.guest_lang  — langue du locataire : l'équipe sait comment l'aborder
--     et le lien du guide part dans la bonne langue (?lang=xx). La liste proposée
--     côté UI se lit dans les langues PUBLIÉES du logement (jamais une liste en
--     dur — offrir une langue non générée créerait une promesse intenable).
--
-- `guest_contact` N'EST PAS supprimée (aucune perte) : tant que les deux nouveaux
-- champs sont vides, la valeur d'origine reste affichée en repli.
--
-- RGPD (§2) : ces deux champs suivent le régime des coordonnées — visibles par
-- l'équipe pour les séjours en cours et à venir uniquement (jamais l'historique).
--
-- Idempotent (IF NOT EXISTS + backfill gardé sur les champs encore NULL), sûr au
-- rejeu.

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS guest_phone TEXT,
    ADD COLUMN IF NOT EXISTS guest_email TEXT,
    ADD COLUMN IF NOT EXISTS guest_lang  TEXT;   -- langue du locataire (code ISO, ex. 'en')

-- Backfill heuristique depuis `guest_contact` (rejouable — ne touche QUE les
-- lignes dont le champ cible est encore NULL) :
--   • valeur contenant '@'                    → guest_email
--   • sinon comportant au moins six chiffres  → guest_phone
--   • ambigu                                  → rien (guest_contact reste en repli)
UPDATE bookings
   SET guest_email = trim(guest_contact)
 WHERE guest_email IS NULL
   AND guest_contact IS NOT NULL
   AND position('@' IN guest_contact) > 0;

UPDATE bookings
   SET guest_phone = trim(guest_contact)
 WHERE guest_phone IS NULL
   AND guest_contact IS NOT NULL
   AND position('@' IN guest_contact) = 0
   AND length(regexp_replace(guest_contact, '[^0-9]', '', 'g')) >= 6;
