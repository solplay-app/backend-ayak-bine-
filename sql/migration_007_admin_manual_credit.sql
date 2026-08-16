-- =====================================================================
-- Migration 007 — Recharge manuelle par l'admin (dépannage / avance,
-- en attendant une vraie intégration opérateur Wave/Orange).
-- =====================================================================

ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'ADMIN_MANUAL';
