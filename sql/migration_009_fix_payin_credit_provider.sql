-- =====================================================================
-- Migration 009 — Corrige process_payin_credit : la variable v_tx ne
-- chargeait pas la colonne `provider`, alors qu'elle est utilisée plus
-- bas (vérification anti-doublon de preuve). PostgreSQL levait
-- "record v_tx has no field provider" à chaque confirmation de dépôt
-- ayant un proof_ref renseigné — donc systématiquement depuis l'admin.
-- CREATE OR REPLACE : sans danger à rejouer, ne touche aucune donnée.
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
    SELECT id, user_id, amount, status, proof_ref, provider
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
