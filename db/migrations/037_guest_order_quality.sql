-- V2-57 Mission A — Plancher de qualité du guide payant : traçabilité de ce qui a
-- été SERVI. La dégradation douce reste la doctrine (un guide amputé/FR-seul se livre
-- quand même), mais un produit à 2,90 € nomme explicitement ce qui manque et pourquoi
-- — plus jamais d'échec muet. `quality_notes` porte le récapitulatif humain (catégories
-- non moissonnées, traductions échouées et leur raison). NULL = rien à signaler.
--
-- Idempotente. Aucun backfill (NULL couvre l'existant).
ALTER TABLE guest_guide_orders ADD COLUMN IF NOT EXISTS quality_notes TEXT;
