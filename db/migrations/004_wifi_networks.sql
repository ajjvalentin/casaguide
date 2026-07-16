-- Migration 004 — Multi-wifi : liste chiffrée de réseaux (M-15)
-- Un logement peut exposer plusieurs réseaux (Maison, Terrasse, Étage…). Ils
-- sont stockés dans UNE colonne chiffrée : la liste JSON [{label, ssid, pass}]
-- est sérialisée puis chiffrée en un seul bytea via l'AES applicatif existant
-- (invariant 5 : clé hors base, jamais de mot de passe en clair côté serveur).
--
-- Idempotent (IF NOT EXISTS). Sur une base fraîche, schema.sql crée déjà la
-- colonne → cette migration est un no-op.
--
-- MIGRATION DES DONNÉES EXISTANTES (l'ancien wifi devient le réseau n°1) :
-- le déchiffrement/re-chiffrement exige la clé AES, qui vit HORS base — il ne
-- peut donc pas se faire en SQL pur. Il est réalisé de façon LAZY côté
-- application : tant que `wifi_networks_enc` est NULL, la lecture des secrets
-- (api/wifi.networks_from_row) synthétise le réseau n°1 { label:"Wifi",
-- ssid:wifi_ssid, pass:decrypt(wifi_pass_enc) } à partir des colonnes legacy ;
-- à la première sauvegarde (PUT /secrets), `wifi_networks_enc` est écrit et les
-- colonnes legacy restent miroir du réseau n°1 (rétrocompatibilité).

ALTER TABLE property_secrets
    ADD COLUMN IF NOT EXISTS wifi_networks_enc BYTEA;  -- liste [{label,ssid,pass}] chiffrée
