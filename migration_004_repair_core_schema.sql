-- =====================================================================
-- Migration 004 — Rattrapage du schéma de base (migration_001 avait été
-- marquée "déjà appliquée" par erreur car la table `users` existait déjà
-- depuis l'ancien projet, mais avec une structure différente : les
-- nouvelles colonnes/tables n'ont donc jamais été créées).
--
-- Entièrement idempotente et non-destructive :
--   - CREATE TYPE protégé par DO $$ ... EXCEPTION duplicate_object
--   - CREATE TABLE IF NOT EXISTS
--   - ALTER TABLE ... ADD COLUMN IF NOT EXISTS pour compléter une table
--     qui existerait déjà avec une structure incomplète (ex: `users`)
--   - CREATE INDEX IF NOT EXISTS
--   - Fonctions en CREATE OR REPLACE (déjà sans risque)
-- Ne supprime et ne modifie jamais une colonne ou une ligne existante.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------
-- Types énumérés
-- ---------------------------------------------------------------------
DO $$
BEGIN
    CREATE TYPE user_role AS ENUM ('USER', 'ADMIN');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE TYPE transaction_type AS ENUM ('PAY_IN', 'PAY_OUT', 'INTERNAL_TRANSFER');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE TYPE transaction_status AS ENUM ('PENDING', 'SUCCESS', 'FAILED', 'CANCELLED', 'REVERSED');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
    CREATE TYPE payment_provider AS ENUM ('WAVE', 'ORANGE_MONEY', 'INTERNAL');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

-- Si le type existait déjà (ex: ancien schéma JEKO avec des valeurs
-- différentes comme WAVE/ORANGE/MTN/MOOV), on complète uniquement les
-- valeurs manquantes attendues par le nouveau code — les anciennes
-- valeurs restent en place, rien n'est supprimé (PostgreSQL ne permet
-- pas de retirer une valeur d'ENUM de toute façon).
ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'WAVE';
ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'ORANGE_MONEY';
ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'INTERNAL';

ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'PAY_IN';
ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'PAY_OUT';
ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'INTERNAL_TRANSFER';

ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'PENDING';
ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'SUCCESS';
ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'FAILED';
ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'CANCELLED';
ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'REVERSED';

ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'USER';
ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'ADMIN';

-- ---------------------------------------------------------------------
-- Comptes utilisateurs — table existante (ancien projet) : on complète
-- uniquement les colonnes manquantes, sans toucher aux comptes déjà là.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number    VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name       VARCHAR(120) NOT NULL DEFAULT 'Utilisateur';
ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_code_hash   VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS role            user_role NOT NULL DEFAULT 'USER';
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active       BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Contrainte UNIQUE sur phone_number si pas déjà présente.
DO $$
BEGIN
    ALTER TABLE users ADD CONSTRAINT users_phone_number_key UNIQUE (phone_number);
EXCEPTION
    WHEN duplicate_table THEN NULL;
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number);

-- ---------------------------------------------------------------------
-- Portefeuilles virtuels
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wallets (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    balance     NUMERIC(15, 2) NOT NULL DEFAULT 0.00 CHECK (balance >= 0),
    currency    VARCHAR(5) NOT NULL DEFAULT 'XOF',
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------
-- Journal comptable (ledger)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger_transactions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    reference           VARCHAR(50) NOT NULL UNIQUE,
    user_id             UUID NOT NULL REFERENCES users(id),
    type                transaction_type NOT NULL,
    provider            payment_provider NOT NULL,
    amount              NUMERIC(15, 2) NOT NULL CHECK (amount > 0),
    fee                 NUMERIC(15, 2) NOT NULL DEFAULT 0.00,
    status              transaction_status NOT NULL DEFAULT 'PENDING',
    phone_number        VARCHAR(20),
    proof_ref           VARCHAR(100),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    ALTER TABLE ledger_transactions
        ADD CONSTRAINT uq_provider_proof UNIQUE NULLS NOT DISTINCT (provider, proof_ref);
EXCEPTION
    WHEN duplicate_table THEN NULL;
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE INDEX IF NOT EXISTS idx_ledger_user         ON ledger_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_ledger_user_created  ON ledger_transactions(user_id, created_at DESC);
-- L'index partiel sur les nouvelles valeurs d'enum (PENDING/PAY_OUT) est
-- créé séparément dans migration_005 : PostgreSQL interdit d'utiliser une
-- valeur d'ENUM tout juste ajoutée (ALTER TYPE ADD VALUE) dans la même
-- transaction — elle doit d'abord être "committée".

-- ---------------------------------------------------------------------
-- Audit log des actions Admin
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    admin_id    UUID NOT NULL REFERENCES users(id),
    action      VARCHAR(50) NOT NULL,
    target_id   UUID,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address  VARCHAR(64),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id, created_at DESC);

-- ---------------------------------------------------------------------
-- Device tokens (notifications push)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_tokens (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token           VARCHAR(255) NOT NULL UNIQUE,
    platform        VARCHAR(20) NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================================
-- FONCTIONS ATOMIQUES — CREATE OR REPLACE, sans risque à rejouer.
-- =====================================================================

CREATE OR REPLACE FUNCTION process_payin_credit(
    p_reference         VARCHAR,
    p_proof_ref         VARCHAR,
    p_confirmed_amount  NUMERIC DEFAULT NULL
) RETURNS JSONB AS $$
DECLARE
    v_tx RECORD;
    v_wallet_balance NUMERIC(15, 2);
    v_amount NUMERIC(15, 2);
BEGIN
    SELECT id, user_id, amount, status, proof_ref
      INTO v_tx
      FROM ledger_transactions
     WHERE reference = p_reference
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Transaction introuvable');
    END IF;
    IF v_tx.status = 'SUCCESS' THEN
        RETURN jsonb_build_object('success', false, 'message', 'Déjà créditée');
    END IF;
    IF v_tx.status NOT IN ('PENDING','FAILED') THEN
        RETURN jsonb_build_object('success', false, 'message', 'Statut non créditable');
    END IF;

    IF p_proof_ref IS NOT NULL AND EXISTS (
        SELECT 1 FROM ledger_transactions
        WHERE proof_ref = p_proof_ref
          AND provider = v_tx.provider
          AND reference <> p_reference
    ) THEN
        RETURN jsonb_build_object('success', false, 'message', 'Preuve déjà utilisée (doublon)');
    END IF;

    IF p_confirmed_amount IS NOT NULL AND p_confirmed_amount <> v_tx.amount THEN
        RETURN jsonb_build_object(
            'success', false,
            'message', 'Montant confirmé ne correspond pas au montant déclaré',
            'declared_amount', v_tx.amount,
            'confirmed_amount', p_confirmed_amount
        );
    END IF;

    v_amount := v_tx.amount;

    SELECT balance INTO v_wallet_balance
      FROM wallets
     WHERE user_id = v_tx.user_id
     FOR UPDATE;

    UPDATE wallets
       SET balance = balance + v_amount,
           updated_at = CURRENT_TIMESTAMP
     WHERE user_id = v_tx.user_id;

    UPDATE ledger_transactions
       SET status          = 'SUCCESS',
           proof_ref       = COALESCE(proof_ref, p_proof_ref),
           updated_at      = CURRENT_TIMESTAMP
     WHERE id = v_tx.id;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Pay-in crédité',
        'transaction_id', v_tx.id,
        'credited_amount', v_amount,
        'new_balance', v_wallet_balance + v_amount
    );
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION process_payout_deduction(
    p_user_id     UUID,
    p_amount      NUMERIC,
    p_reference   VARCHAR,
    p_provider    payment_provider,
    p_phone       VARCHAR
) RETURNS JSONB AS $$
DECLARE
    v_balance NUMERIC(15, 2);
    v_tx_id   UUID;
BEGIN
    SELECT balance INTO v_balance
      FROM wallets
     WHERE user_id = p_user_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Portefeuille inexistant');
    END IF;

    IF v_balance < p_amount THEN
        RETURN jsonb_build_object('success', false, 'message', 'Solde virtuel insuffisant');
    END IF;

    UPDATE wallets
       SET balance   = balance - p_amount,
           updated_at = CURRENT_TIMESTAMP
     WHERE user_id   = p_user_id;

    INSERT INTO ledger_transactions
        (reference, user_id, type, provider, amount, status, phone_number, metadata)
    VALUES
        (p_reference, p_user_id, 'PAY_OUT', p_provider, p_amount, 'PENDING', p_phone,
         jsonb_build_object('requested_at', CURRENT_TIMESTAMP))
    RETURNING id INTO v_tx_id;

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Retrait réservé',
        'reference', p_reference,
        'transaction_id', v_tx_id,
        'new_balance', v_balance - p_amount
    );
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION admin_process_payout(
    p_tx_id      UUID,
    p_action     VARCHAR,
    p_proof_ref  VARCHAR DEFAULT NULL,
    p_admin_id   UUID DEFAULT NULL
) RETURNS JSONB AS $$
DECLARE
    v_tx     RECORD;
    v_amount NUMERIC(15, 2);
    v_user   UUID;
BEGIN
    SELECT * INTO v_tx FROM ledger_transactions WHERE id = p_tx_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Transaction introuvable');
    END IF;
    IF v_tx.status <> 'PENDING' THEN
        RETURN jsonb_build_object('success', false, 'message', 'Déjà traitée', 'status', v_tx.status);
    END IF;

    v_amount := v_tx.amount;
    v_user   := v_tx.user_id;

    IF p_action = 'APPROVE' THEN
        UPDATE ledger_transactions
           SET status = 'SUCCESS',
               proof_ref = COALESCE(p_proof_ref, proof_ref),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = p_tx_id;

        INSERT INTO admin_audit_log (admin_id, action, target_id, details)
        VALUES (p_admin_id, 'APPROVE_PAYOUT', p_tx_id,
                jsonb_build_object('proof_ref', p_proof_ref, 'amount', v_amount));

        RETURN jsonb_build_object('success', true, 'message', 'Pay-out validé');

    ELSIF p_action = 'REJECT' THEN
        UPDATE wallets
           SET balance = balance + v_amount,
               updated_at = CURRENT_TIMESTAMP
         WHERE user_id = v_user;

        UPDATE ledger_transactions
           SET status = 'CANCELLED',
               updated_at = CURRENT_TIMESTAMP,
               metadata = metadata || jsonb_build_object('rejected_by', p_admin_id::TEXT)
         WHERE id = p_tx_id;

        INSERT INTO admin_audit_log (admin_id, action, target_id, details)
        VALUES (p_admin_id, 'REJECT_PAYOUT', p_tx_id,
                jsonb_build_object('refunded_amount', v_amount));

        RETURN jsonb_build_object('success', true, 'message', 'Pay-out rejeté, solde remboursé');
    ELSE
        RETURN jsonb_build_object('success', false, 'message', 'Action inconnue');
    END IF;
END;
$$ LANGUAGE plpgsql;
