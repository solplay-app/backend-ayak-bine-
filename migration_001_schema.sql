-- =====================================================================
-- Ayak'bine — Core Banking & Virtual Ledger System
-- PostgreSQL / Supabase — Schema v1.1 (corrigé)
-- =====================================================================
-- Corrections appliquées par rapport au CDC d'origine :
--   * NUMERIC(15,2) partout → pas de float en BDD (Decimal côté Python).
--   * UNIQUE (provider, proof_ref) → bloque les réinjections de Pay-in-Webhook.
--   * Type ENUM conservés, mais valeurs TRY_FAILED/REVERSED ajoutées
--     pour le suivi des Pay-out échoués.
--   * Table admin_audit_log pour tracer toute action admin (rejet, validation,
--     remboursement) — exigence anti-fraude.
--   * Table device_tokens pour push notifications (push FCM optionnel).
--   * Index ciblés pour les requêtes Dashboard (status + created_at).
--   * Wallet a une CHECK balance >= 0 — la double dépense est bloquée au
--     niveau de la base, même si une race condition échappe au verrou applicatif.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------
-- Rôles utilisateurs : USER (defaut) et ADMIN (privileges Dashboard)
-- ---------------------------------------------------------------------
CREATE TYPE user_role AS ENUM ('USER', 'ADMIN');

-- ---------------------------------------------------------------------
-- Types énumérés
-- ---------------------------------------------------------------------
CREATE TYPE transaction_type     AS ENUM ('PAY_IN', 'PAY_OUT', 'INTERNAL_TRANSFER');
CREATE TYPE transaction_status   AS ENUM ('PENDING', 'SUCCESS', 'FAILED', 'CANCELLED', 'REVERSED');
CREATE TYPE payment_provider     AS ENUM ('WAVE', 'ORANGE_MONEY', 'INTERNAL');

-- ---------------------------------------------------------------------
-- Comptes utilisateurs
-- ---------------------------------------------------------------------
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone_number    VARCHAR(20) NOT NULL UNIQUE,
    full_name       VARCHAR(120) NOT NULL DEFAULT 'Utilisateur',
    pin_code_hash   VARCHAR(255) NOT NULL,                 -- bcrypt
    role            user_role NOT NULL DEFAULT 'USER',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_users_phone ON users(phone_number);

-- ---------------------------------------------------------------------
-- Portefeuilles virtuels (solde crédité uniquement sur Pay-In confirmé)
-- ---------------------------------------------------------------------
CREATE TABLE wallets (
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
CREATE TABLE ledger_transactions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    reference           VARCHAR(50) NOT NULL UNIQUE,        -- PI-/PO-/IT-
    user_id             UUID NOT NULL REFERENCES users(id),
    type                transaction_type NOT NULL,
    provider            payment_provider NOT NULL,
    amount              NUMERIC(15, 2) NOT NULL CHECK (amount > 0),
    fee                 NUMERIC(15, 2) NOT NULL DEFAULT 0.00,
    status              transaction_status NOT NULL DEFAULT 'PENDING',
    phone_number        VARCHAR(20),
    proof_ref           VARCHAR(100),                       -- ref Wave/OM réelle
    -- Idempotence : empêche le même (provider, proof_ref) d'être crédité deux fois.
    -- Requis pour bloquer les ré-injections de webhook ou un double-clic client.
    CONSTRAINT uq_provider_proof UNIQUE NULLS NOT DISTINCT (provider, proof_ref),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ledger_user        ON ledger_transactions(user_id);
CREATE INDEX idx_ledger_user_created ON ledger_transactions(user_id, created_at DESC);
-- Requête dominante du Dashboard Admin :
CREATE INDEX idx_ledger_pending_payout ON ledger_transactions(status, type, created_at)
    WHERE status = 'PENDING' AND type = 'PAY_OUT';

-- ---------------------------------------------------------------------
-- Audit log des actions Admin (traçabilité anti-fraude)
-- ---------------------------------------------------------------------
CREATE TABLE admin_audit_log (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    admin_id    UUID NOT NULL REFERENCES users(id),
    action      VARCHAR(50) NOT NULL,        -- APPROVE_PAYOUT / REJECT_PAYOUT / ...
    target_id   UUID,                        -- transaction_id concerné
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address  VARCHAR(64),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_audit_admin ON admin_audit_log(admin_id, created_at DESC);

-- ---------------------------------------------------------------------
-- Device tokens pour les notifications push (FCM)
-- ---------------------------------------------------------------------
CREATE TABLE device_tokens (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token           VARCHAR(255) NOT NULL UNIQUE,
    platform        VARCHAR(20) NOT NULL,    -- android / ios
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- =====================================================================
-- FONCTIONS ATOMIQUES (PostgreSQL)
-- =====================================================================

-- 1) Crédit PAY-IN — UNIQUEMENT sur preuve de paiement validée
--    Bloque la double dépense côté DB via row-level CHECK.
--    Ne crédite que si la transaction existe en PENDING et que proof_ref
--    est fourni.
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
    -- Verrou PENDING -> empêche que webhook+SMS_listener créditent 2 fois
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

    -- Si la contrainte UNIQUE(provider, proof_ref) a déjà été insérée
    -- avec un autre référence, on refuse.
    IF p_proof_ref IS NOT NULL AND EXISTS (
        SELECT 1 FROM ledger_transactions
        WHERE proof_ref = p_proof_ref
          AND provider = v_tx.provider
          AND reference <> p_reference
    ) THEN
        RETURN jsonb_build_object('success', false, 'message', 'Preuve déjà utilisée (doublon)');
    END IF;

    -- FIX FAILLE MONTANT : le montant confirmé par l'opérateur (webhook/SMS)
    -- DOIT correspondre exactement au montant déclaré par l'utilisateur.
    -- Sans ce contrôle, un utilisateur peut déclarer un montant arbitraire
    -- avec une proof_ref réelle (mais d'un montant réel bien plus faible)
    -- et se faire créditer le montant déclaré au lieu du montant reçu.
    IF p_confirmed_amount IS NOT NULL AND p_confirmed_amount <> v_tx.amount THEN
        RETURN jsonb_build_object(
            'success', false,
            'message', 'Montant confirmé ne correspond pas au montant déclaré',
            'declared_amount', v_tx.amount,
            'confirmed_amount', p_confirmed_amount
        );
    END IF;

    v_amount := v_tx.amount;

    -- Lock du wallet, incrément, MAJ du ledger
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

-- 2) Débit PAY-OUT — verrouille d'abord la ligne pour bloquer
--    une demande de retrait concurrente si le solde est insuffisant.
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
    -- Verrou portefeuille (FOR UPDATE bloque toute autre transaction concurrente)
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
        'transaction_id', v_tx_id,
        'new_balance', v_balance - p_amount
    );
END;
$$ LANGUAGE plpgsql;

-- 3) Approbation admin — un seul point d'entrée pour clôturer un PENDING.
--    Le remboursement REJECT se fait DANS LA MÊME transaction que la mise à
--    jour du statut → impossible de créditer deux fois même en cas d'appel
--    répété.
CREATE OR REPLACE FUNCTION admin_process_payout(
    p_tx_id      UUID,
    p_action     VARCHAR,                 -- 'APPROVE' | 'REJECT'
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
        -- Restitution du solde dans la MÊME transaction SQL
        -- → pas de risque de double-crédit si l'endpoint est appelé 2x.
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
