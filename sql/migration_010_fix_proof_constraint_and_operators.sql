-- =====================================================================
-- Migration 010 — Deux correctifs :
--
-- 1) uq_provider_proof (migration_004) utilisait NULLS NOT DISTINCT :
--    PostgreSQL traitait alors TOUTES les lignes avec proof_ref NULL
--    d'un même provider comme des doublons entre elles. Résultat :
--    - Tout retrait (PAY_OUT) au-delà du premier, pour un même provider,
--      échouait ("duplicate key uq_provider_proof").
--    - TOUT transfert interne échouait, y compris le tout premier : un
--      transfert crée 2 lignes (expéditeur + destinataire), toutes deux
--      provider=INTERNAL / proof_ref=NULL, qui entraient en conflit
--      l'une avec l'autre dans la même transaction.
--    Correctif : remplacer par un index unique PARTIEL qui ne s'applique
--    que si proof_ref IS NOT NULL — seul cas où on veut vraiment
--    empêcher la réutilisation d'une preuve de paiement.
--
-- 2) Le type ENUM payment_provider ne contenait pas 'ORANGE', 'MTN',
--    'MOOV' — alors que l'app propose ces 4 opérateurs partout (dépôt
--    et retrait). Seuls WAVE/ORANGE_MONEY/INTERNAL/ADMIN_MANUAL
--    existaient. On complète.
-- =====================================================================

-- --- 1) Contrainte anti-doublon corrigée -------------------------------
ALTER TABLE ledger_transactions DROP CONSTRAINT IF EXISTS uq_provider_proof;
DROP INDEX IF EXISTS uq_provider_proof_partial;

CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_proof_partial
    ON ledger_transactions (provider, proof_ref)
    WHERE proof_ref IS NOT NULL;

-- --- 2) Valeurs d'enum manquantes --------------------------------------
ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'ORANGE';
ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'MTN';
ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'MOOV';
