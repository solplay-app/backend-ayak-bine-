-- =====================================================================
-- Migration 002 — KYC + Transfert interne (endpoints manquants côté app)
-- À exécuter APRÈS sql/schema.sql (ajouts additifs, ne modifie rien
-- d'existant).
-- Idempotente : peut être exécutée plusieurs fois / rejouée sur une base
-- où le type/table/index existeraient déjà (ex: créés hors de ce script)
-- sans provoquer d'erreur DuplicateObject qui bloquerait le déploiement.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) Vérification d'identité (KYC)
-- ---------------------------------------------------------------------
-- CREATE TYPE ne supporte pas IF NOT EXISTS en PostgreSQL : on protège
-- donc la création via un bloc DO qui ignore l'erreur si le type existe
-- déjà (cas d'une base où il aurait été créé hors de ce script).
DO $$
BEGIN
    CREATE TYPE kyc_status AS ENUM ('PENDING', 'APPROVED', 'REJECTED');
EXCEPTION
    WHEN duplicate_object THEN
        NULL;
END
$$;

CREATE TABLE IF NOT EXISTS kyc_submissions (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id              UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id_document_base64   TEXT NOT NULL,
    selfie_base64        TEXT,
    status               kyc_status NOT NULL DEFAULT 'PENDING',
    reviewed_by          UUID REFERENCES users(id),
    review_note          TEXT,
    created_at           TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Si kyc_submissions existait déjà (créée hors de ce script, avec une
-- structure différente/incomplète), le CREATE TABLE IF NOT EXISTS
-- ci-dessus ne la modifie pas. On complète donc les colonnes manquantes
-- une par une, sans jamais toucher aux colonnes déjà présentes.
ALTER TABLE kyc_submissions ADD COLUMN IF NOT EXISTS selfie_base64      TEXT;
ALTER TABLE kyc_submissions ADD COLUMN IF NOT EXISTS reviewed_by        UUID REFERENCES users(id);
ALTER TABLE kyc_submissions ADD COLUMN IF NOT EXISTS review_note        TEXT;
ALTER TABLE kyc_submissions ADD COLUMN IF NOT EXISTS created_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE kyc_submissions ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Un seul dossier "actif" (PENDING ou APPROVED) par utilisateur à la fois.
CREATE UNIQUE INDEX IF NOT EXISTS uq_kyc_active_per_user
    ON kyc_submissions(user_id)
    WHERE status IN ('PENDING', 'APPROVED');

CREATE INDEX IF NOT EXISTS idx_kyc_status ON kyc_submissions(status, created_at DESC);

-- ---------------------------------------------------------------------
-- 2) Transfert interne (wallet à wallet, entre utilisateurs Ayak'bine)
--    Débit + crédit dans UNE seule transaction SQL — impossible de
--    perdre ou dupliquer de l'argent, même en cas de crash applicatif.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION process_internal_transfer(
    p_sender_id        UUID,
    p_amount           NUMERIC,
    p_recipient_phone  VARCHAR,
    p_sender_reference    VARCHAR,
    p_recipient_reference VARCHAR
) RETURNS JSONB AS $$
DECLARE
    v_sender_balance    NUMERIC(15, 2);
    v_recipient_id      UUID;
    v_recipient_wallet  UUID;
BEGIN
    IF p_amount <= 0 THEN
        RETURN jsonb_build_object('success', false, 'message', 'Montant invalide');
    END IF;

    -- Résout le destinataire par numéro de téléphone.
    SELECT id INTO v_recipient_id FROM users WHERE phone_number = p_recipient_phone;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Destinataire introuvable');
    END IF;
    IF v_recipient_id = p_sender_id THEN
        RETURN jsonb_build_object('success', false, 'message', 'Impossible de se transférer à soi-même');
    END IF;

    -- Verrouille les DEUX portefeuilles dans un ordre déterministe
    -- (par UUID trié) pour éviter tout deadlock entre transferts croisés.
    PERFORM 1 FROM wallets WHERE user_id IN (p_sender_id, v_recipient_id)
        ORDER BY user_id FOR UPDATE;

    SELECT balance INTO v_sender_balance FROM wallets WHERE user_id = p_sender_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Portefeuille expéditeur introuvable');
    END IF;
    IF v_sender_balance < p_amount THEN
        RETURN jsonb_build_object('success', false, 'message', 'Solde virtuel insuffisant');
    END IF;

    SELECT id INTO v_recipient_wallet FROM wallets WHERE user_id = v_recipient_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Portefeuille destinataire introuvable');
    END IF;

    UPDATE wallets SET balance = balance - p_amount, updated_at = CURRENT_TIMESTAMP
     WHERE user_id = p_sender_id;
    UPDATE wallets SET balance = balance + p_amount, updated_at = CURRENT_TIMESTAMP
     WHERE user_id = v_recipient_id;

    INSERT INTO ledger_transactions
        (reference, user_id, type, provider, amount, status, phone_number, metadata)
    VALUES
        (p_sender_reference, p_sender_id, 'INTERNAL_TRANSFER', 'INTERNAL', p_amount, 'SUCCESS',
         p_recipient_phone, jsonb_build_object('direction', 'OUT', 'counterparty_user_id', v_recipient_id));

    INSERT INTO ledger_transactions
        (reference, user_id, type, provider, amount, status, phone_number, metadata)
    VALUES
        (p_recipient_reference, v_recipient_id, 'INTERNAL_TRANSFER', 'INTERNAL', p_amount, 'SUCCESS',
         (SELECT phone_number FROM users WHERE id = p_sender_id),
         jsonb_build_object('direction', 'IN', 'counterparty_user_id', p_sender_id));

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Transfert effectué',
        'sender_reference', p_sender_reference,
        'recipient_reference', p_recipient_reference,
        'new_balance', v_sender_balance - p_amount
    );
END;
$$ LANGUAGE plpgsql;
