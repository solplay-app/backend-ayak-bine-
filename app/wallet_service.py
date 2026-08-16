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


def get_fee_percent(db: Session) -> Decimal:
    """Lit le pourcentage de frais plateforme actuel (réglable par l'admin)."""
    row = db.execute(
        text("SELECT value FROM platform_settings WHERE key = 'fee_percent'")
    ).first()
    return Decimal(row[0]) if row else Decimal("8")


def set_fee_percent(db: Session, percent: Decimal) -> None:
    db.execute(
        text("""
            INSERT INTO platform_settings (key, value, updated_at)
            VALUES ('fee_percent', :v, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = CURRENT_TIMESTAMP
        """),
        {"v": str(percent)},
    )
    db.commit()


def transfer_internal(
    db: Session,
    *,
    sender_id: _uuid.UUID,
    amount: Decimal,
    recipient_phone: str,
) -> dict[str, Any]:
    """Appelle la fonction SQL atomique process_internal_transfer (débit + crédit).

    Le frais plateforme (%) est ajouté au montant : l'expéditeur paie
    amount + frais, le destinataire reçoit `amount` plein.
    """
    sender_reference = generate_reference("IT")
    recipient_reference = generate_reference("IT")
    fee_percent = get_fee_percent(db)
    result = db.execute(
        text("""
            SELECT process_internal_transfer(
                :sender_id, :amount, :recipient_phone,
                :sender_ref, :recipient_ref, :fee_percent
            ) AS r
        """),
        {
            "sender_id": sender_id,
            "amount": amount,
            "recipient_phone": recipient_phone,
            "sender_ref": sender_reference,
            "recipient_ref": recipient_reference,
            "fee_percent": fee_percent,
        },
    ).scalar_one()
    db.commit()
    return dict(result)


_PAYMENT_LINK_KEYS = {
    "wave": "payment_link_wave",
    "orange": "payment_link_orange",
    "mtn": "payment_link_mtn",
    "moov": "payment_link_moov",
}


def get_payment_links(db: Session) -> dict[str, str | None]:
    """Lit les liens de paiement marchand configurés par l'admin (Wave,
    Orange Money, etc.) — utilisés par l'app pour rediriger le client au
    moment du dépôt, en attendant une vraie intégration API opérateur.
    """
    rows = db.execute(
        text("SELECT key, value FROM platform_settings WHERE key = ANY(:keys)"),
        {"keys": list(_PAYMENT_LINK_KEYS.values())},
    ).fetchall()
    by_key = {r[0]: r[1] for r in rows}
    return {name: by_key.get(dbkey) or None for name, dbkey in _PAYMENT_LINK_KEYS.items()}


def set_payment_links(db: Session, links: dict[str, str | None]) -> None:
    for name, value in links.items():
        if name not in _PAYMENT_LINK_KEYS or value is None:
            continue
        db.execute(
            text("""
                INSERT INTO platform_settings (key, value, updated_at)
                VALUES (:k, :v, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = CURRENT_TIMESTAMP
            """),
            {"k": _PAYMENT_LINK_KEYS[name], "v": value},
        )
    db.commit()


def admin_manual_credit(
    db: Session,
    *,
    user_id: str,
    amount: Decimal,
    reason: str,
    admin_id: _uuid.UUID,
) -> dict[str, Any]:
    """Recharge manuelle par l'admin — dépannage, remboursement commercial,
    ou avance de trésorerie avant qu'une vraie intégration opérateur existe.
    Crédite immédiatement le wallet, trace l'opération dans le ledger.
    """
    if amount <= 0:
        return {"success": False, "message": "Le montant doit être positif."}

    reference = generate_reference("MC")
    row = db.execute(
        text("""
            UPDATE wallets SET balance = balance + :amount, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = :uid
            RETURNING balance
        """),
        {"amount": amount, "uid": user_id},
    ).first()
    if row is None:
        return {"success": False, "message": "Portefeuille introuvable pour cet utilisateur."}

    db.execute(
        text("""
            INSERT INTO ledger_transactions
                (reference, user_id, type, provider, amount, fee, status, metadata)
            VALUES
                (:ref, :uid, 'PAY_IN', 'ADMIN_MANUAL', :amount, 0, 'SUCCESS', :meta)
        """),
        {
            "ref": reference,
            "uid": user_id,
            "amount": amount,
            "meta": json.dumps({"reason": reason, "credited_by_admin": str(admin_id)}),
        },
    )
    db.commit()
    return {"success": True, "message": "Compte rechargé avec succès.", "reference": reference, "new_balance": str(row[0])}


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
