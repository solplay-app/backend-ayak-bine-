"""Logique métier du portefeuille : utilise les fonctions SQL atomiques."""
from __future__ import annotations
import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

import uuid as _uuid


def generate_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def declare_payin_pending(
    db: Session,
    *,
    user_id: _uuid.UUID,
    amount: Decimal,
    provider: str,
    phone_number: str,
    proof_ref: str | None,
    declared_via: str,
) -> dict[str, Any]:
    """Crée la ligne PENDING côté ledger — pas de crédit avant validation webhook/SMS."""
    reference = generate_reference("PI")
    provider_enum = provider.upper()  # 'WAVE' / 'ORANGE_MONEY'
    db.execute(
        text("""
            INSERT INTO ledger_transactions
              (reference, user_id, type, provider, amount, status, phone_number, proof_ref, metadata)
            VALUES
              (:ref, :uid, 'PAY_IN', CAST(:prov AS payment_provider),
               :amt, 'PENDING', :phone, :proof,
               CAST(:meta AS JSONB))
            ON CONFLICT (provider, proof_ref) DO NOTHING
            RETURNING id, reference
        """),
        {
            "ref": reference,
            "uid": user_id,
            "prov": provider_enum,
            "amt": amount,
            "phone": phone_number,
            "proof": proof_ref,
            "meta": json.dumps({"declared_via": declared_via}),
        },
    )
    row = db.execute(
        text("SELECT id, reference FROM ledger_transactions WHERE reference = :r"),
        {"r": reference},
    ).first()
    db.commit()
    if not row:
        # preuve déjà présente → récupère la transaction existante
        existing = db.execute(
            text("""
                SELECT id, reference FROM ledger_transactions
                WHERE provider = CAST(:prov AS payment_provider)
                  AND proof_ref = :proof
            """),
            {"prov": provider_enum, "proof": proof_ref},
        ).first()
        return {
            "success": False,
            "message": "Doublon détecté (preuve déjà utilisée)",
            "reference": existing.reference if existing else None,
            "transaction_id": existing.id if existing else None,
        }
    return {
        "success": True,
        "message": "Déclaration enregistrée",
        "reference": row.reference,
        "transaction_id": row.id,
    }


def request_payout(
    db: Session,
    *,
    user_id: _uuid.UUID,
    amount: Decimal,
    provider: str,
    phone_number: str,
) -> dict[str, Any]:
    """Appelle la fonction SQL atomique process_payout_deduction."""
    reference = generate_reference("PO")
    result = db.execute(
        text("""
            SELECT process_payout_deduction(
                :uid, :amt, :ref,
                CAST(:prov AS payment_provider), :phone
            ) AS r
        """),
        {
            "uid": user_id,
            "amt": amount,
            "ref": reference,
            "prov": provider.upper(),
            "phone": phone_number,
        },
    ).scalar_one()
    db.commit()
    return dict(result)


def admin_process_payout(
    db: Session,
    *,
    transaction_id: _uuid.UUID,
    action: str,
    proof_ref: str | None,
    admin_id: _uuid.UUID,
    ip: str | None = None,
) -> dict[str, Any]:
    """Route admin unique — APPROVE / REJECT — reembolso atomique côté SQL."""
    result = db.execute(
        text("""
            SELECT admin_process_payout(
                :tid,
                :action,
                :proof,
                :aid
            ) AS r
        """),
        {
            "tid": transaction_id,
            "action": action,
            "proof": proof_ref,
            "aid": admin_id,
        },
    ).scalar_one()
    db.commit()
    payload = dict(result)
    # Log IP côté audit (déjà dans la fonction SQL via admin_audit_log)
    if ip:
        db.execute(
            text("""
                UPDATE admin_audit_log
                   SET ip_address = :ip
                 WHERE admin_id = :aid
                   AND target_id = :tid
                   AND created_at = (
                       SELECT MAX(created_at) FROM admin_audit_log
                        WHERE admin_id = :aid AND target_id = :tid
                   )
            """),
            {"ip": ip, "aid": admin_id, "tid": transaction_id},
        )
        db.commit()
    return payload


def transfer_internal(
    db: Session,
    *,
    sender_id: _uuid.UUID,
    amount: Decimal,
    recipient_phone: str,
) -> dict[str, Any]:
    """Appelle la fonction SQL atomique process_internal_transfer (débit + crédit)."""
    sender_reference = generate_reference("IT")
    recipient_reference = generate_reference("IT")
    result = db.execute(
        text("""
            SELECT process_internal_transfer(
                :sender_id, :amount, :recipient_phone,
                :sender_ref, :recipient_ref
            ) AS r
        """),
        {
            "sender_id": sender_id,
            "amount": amount,
            "recipient_phone": recipient_phone,
            "sender_ref": sender_reference,
            "recipient_ref": recipient_reference,
        },
    ).scalar_one()
    db.commit()
    return dict(result)


def finalize_payin(
    db: Session,
    *,
    reference: str,
    proof_ref: str,
    confirmed_amount: Decimal | None = None,
) -> dict[str, Any]:
    """Appelle process_payin_credit — atomique, idempotent.

    confirmed_amount = montant vu dans le webhook/SMS opérateur.
    Si fourni, la fonction SQL refuse de créditer si ça ne correspond
    pas exactement au montant déclaré par l'utilisateur (anti-fraude).
    """
    row = db.execute(
        text("SELECT process_payin_credit(:ref, :proof, :amt) AS r"),
        {"ref": reference, "proof": proof_ref, "amt": confirmed_amount},
    ).scalar_one()
    db.commit()
    return dict(row)
