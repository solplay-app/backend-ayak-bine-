-- =====================================================================
-- Migration 006 — Frais plateforme configurables (%) + tableau de bord.
-- Idempotente, additive, ne touche à aucune donnée existante.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) Réglages plateforme (clé/valeur) — permet à l'admin de changer le
--    pourcentage de frais sans redéployer le backend.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform_settings (
    key         VARCHAR(50) PRIMARY KEY,
    value       VARCHAR(50) NOT NULL,
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO platform_settings (key, value)
VALUES ('fee_percent', '8')
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2) Transfert interne avec frais plateforme.
--    Le frais (%) est AJOUTÉ au montant voulu : l'expéditeur paie
--    montant + frais, le destinataire reçoit le montant plein (net).
--    Le frais prélevé est stocké dans la colonne `fee` de la ligne
--    ledger de l'EXPÉDITEUR (jamais déduit du destinataire).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION process_internal_transfer(
    p_sender_id        UUID,
    p_amount           NUMERIC,
    p_recipient_phone  VARCHAR,
    p_sender_reference    VARCHAR,
    p_recipient_reference VARCHAR,
    p_fee_percent      NUMERIC DEFAULT 8
) RETURNS JSONB AS $$
DECLARE
    v_sender_balance    NUMERIC(15, 2);
    v_recipient_id      UUID;
    v_recipient_wallet  UUID;
    v_fee               NUMERIC(15, 2);
    v_total             NUMERIC(15, 2);
BEGIN
    IF p_amount <= 0 THEN
        RETURN jsonb_build_object('success', false, 'message', 'Montant invalide');
    END IF;

    v_fee   := ROUND(p_amount * p_fee_percent / 100.0, 2);
    v_total := p_amount + v_fee;

    SELECT id INTO v_recipient_id FROM users WHERE phone_number = p_recipient_phone;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Destinataire introuvable');
    END IF;
    IF v_recipient_id = p_sender_id THEN
        RETURN jsonb_build_object('success', false, 'message', 'Impossible de se transférer à soi-même');
    END IF;

    PERFORM 1 FROM wallets WHERE user_id IN (p_sender_id, v_recipient_id)
        ORDER BY user_id FOR UPDATE;

    SELECT balance INTO v_sender_balance FROM wallets WHERE user_id = p_sender_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Portefeuille expéditeur introuvable');
    END IF;
    IF v_sender_balance < v_total THEN
        RETURN jsonb_build_object(
            'success', false,
            'message', 'Solde virtuel insuffisant (montant + frais requis : ' || v_total || ' FCFA)'
        );
    END IF;

    SELECT id INTO v_recipient_wallet FROM wallets WHERE user_id = v_recipient_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('success', false, 'message', 'Portefeuille destinataire introuvable');
    END IF;

    UPDATE wallets SET balance = balance - v_total, updated_at = CURRENT_TIMESTAMP
     WHERE user_id = p_sender_id;
    UPDATE wallets SET balance = balance + p_amount, updated_at = CURRENT_TIMESTAMP
     WHERE user_id = v_recipient_id;

    INSERT INTO ledger_transactions
        (reference, user_id, type, provider, amount, fee, status, phone_number, metadata)
    VALUES
        (p_sender_reference, p_sender_id, 'INTERNAL_TRANSFER', 'INTERNAL', p_amount, v_fee, 'SUCCESS',
         p_recipient_phone, jsonb_build_object('role', 'sender', 'fee_percent', p_fee_percent));

    INSERT INTO ledger_transactions
        (reference, user_id, type, provider, amount, fee, status, phone_number, metadata)
    VALUES
        (p_recipient_reference, v_recipient_id, 'INTERNAL_TRANSFER', 'INTERNAL', p_amount, 0, 'SUCCESS',
         (SELECT phone_number FROM users WHERE id = p_sender_id), jsonb_build_object('role', 'recipient'));

    RETURN jsonb_build_object(
        'success', true,
        'message', 'Transfert effectué',
        'sender_reference', p_sender_reference,
        'recipient_reference', p_recipient_reference,
        'net_amount', p_amount,
        'fee', v_fee,
        'total_charged', v_total,
        'new_balance', v_sender_balance - v_total
    );
END;
$$ LANGUAGE plpgsql;
