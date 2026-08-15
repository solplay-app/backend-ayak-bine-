-- =====================================================================
-- Migration 005 — Index partiel sur les nouvelles valeurs d'enum
-- (PENDING / PAY_OUT). Séparée de migration_004 car PostgreSQL interdit
-- d'utiliser une valeur tout juste ajoutée via ALTER TYPE ... ADD VALUE
-- dans la même transaction que son ajout — elle doit d'abord être
-- "committée". migration_004 s'exécute dans sa propre transaction et se
-- termine (commit) avant que ce fichier ne démarre la sienne.
-- Idempotente : CREATE INDEX IF NOT EXISTS.
-- =====================================================================

CREATE INDEX IF NOT EXISTS idx_ledger_pending_payout ON ledger_transactions(status, type, created_at)
    WHERE status = 'PENDING' AND type = 'PAY_OUT';
