-- =====================================================================
-- Migration 003 — Correctif : process_payout_deduction ne renvoyait pas
-- la référence de transaction générée (le champ 'reference' était donc
-- toujours null côté app Flutter après un retrait).
-- Idempotent : peut être exécutée plusieurs fois sans risque.
-- =====================================================================

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
