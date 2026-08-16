-- =====================================================================
-- Migration 008 — Liens de paiement marchand (Wave, Orange Money...).
-- La colonne `value` de platform_settings était en VARCHAR(50), trop
-- courte pour une URL de paiement marchand. On l'élargit en TEXT.
-- =====================================================================

ALTER TABLE platform_settings ALTER COLUMN value TYPE TEXT;
