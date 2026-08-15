"""/payin/declare, /payin/status, /payout/request, /me, /transactions."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db, get_sync_db
from app.models import LedgerTransaction, PaymentProvider, TransactionStatus, TransactionType, User, Wallet
from app.schemas import (
    FeePercentResponse, InternalTransferRequest, InternalTransferResult,
    PayInDeclareRequest, PayInResult, PayOutRequest, TransactionOut, WalletOut,
)
from app.wallet_service import declare_payin_pending, generate_reference, get_fee_percent, request_payout, transfer_internal

router = APIRouter(prefix="/api/v1/wallet", tags=["Wallet"])


@router.get("/me", response_model=WalletOut)
async def get_my_wallet(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user.id))
    ).scalar_one_or_none()
    if not wallet:
        raise HTTPException(404, "Wallet introuvable")
    return WalletOut(
        user_phone=user.phone_number,
        balance=Decimal(wallet.balance),
        currency=wallet.currency,
        updated_at=wallet.updated_at,
    )


@router.post("/payin/declare", response_model=PayInResult)
async def declare_payin(
    payload: PayInDeclareRequest,
    user: User = Depends(get_current_user),
    db: Annotated[Session, Depends(get_sync_db)] = None,  # noqa
):
    if payload.provider.upper() not in ("WAVE", "ORANGE_MONEY"):
        raise HTTPException(400, "Fournisseur de paiement invalide")
    result = declare_payin_pending(
        db,
        user_id=user.id,
        amount=payload.amount,
        provider=payload.provider.upper(),
        phone_number=payload.phone_number,
        proof_ref=payload.proof_ref,
        declared_via=payload.declared_via,
    )
    if not result["success"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result["message"])
    return PayInResult(
        success=True,
        message=result["message"],
        reference=result["reference"],
        transaction_id=result["transaction_id"],
    )


@router.post("/payin/declare-async", status_code=202)
async def declare_payin_async(
    payload: PayInDeclareRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Variante async — utile pour de gros volumes ou si on bascule
    en async psycopg uniquement.
    """
    if payload.provider.upper() not in ("WAVE", "ORANGE_MONEY"):
        raise HTTPException(400, "Fournisseur de paiement invalide")
    reference = generate_reference("PI")
    tx = LedgerTransaction(
        reference=reference,
        user_id=user.id,
        type=TransactionType.PAY_IN,
        provider=PaymentProvider(payload.provider.upper()),
        amount=payload.amount,
        status=TransactionStatus.PENDING,
        phone_number=payload.phone_number,
        proof_ref=payload.proof_ref,
        metadata_={"declared_via": payload.declared_via, "declared_at": datetime.now(timezone.utc).isoformat()},
    )
    db.add(tx)
    try:
        await db.commit()
    except Exception as e:  # UNIQUE(provider, proof_ref) ou autre
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Conflit lors de l'enregistrement: {e}") from e
    return {"success": True, "reference": reference, "transaction_id": str(tx.id)}


@router.post("/payout/request")
async def payout_request(
    payload: PayOutRequest,
    user: User = Depends(get_current_user),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    if payload.provider.upper() not in ("WAVE", "ORANGE_MONEY"):
        raise HTTPException(400, "Fournisseur de paiement invalide")
    result = request_payout(
        db,
        user_id=user.id,
        amount=payload.amount,
        provider=payload.provider.upper(),
        phone_number=payload.phone_number,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/fee-percent", response_model=FeePercentResponse)
async def get_current_fee_percent(
    user: User = Depends(get_current_user),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    """Pourcentage de frais plateforme actuel — utilisé par l'app pour l'affichage en temps réel."""
    return FeePercentResponse(fee_percent=get_fee_percent(db))


@router.post("/transfer/internal", response_model=InternalTransferResult)
async def transfer_internal_endpoint(
    payload: InternalTransferRequest,
    user: User = Depends(get_current_user),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    """Transfert wallet à wallet entre deux utilisateurs Ayak'bine (aucun agrégateur)."""
    result = transfer_internal(
        db,
        sender_id=user.id,
        amount=payload.amount,
        recipient_phone=payload.recipient_phone,
    )
    if not result["success"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["message"])
    return InternalTransferResult(
        success=True,
        message=result["message"],
        sender_reference=result.get("sender_reference"),
        recipient_reference=result.get("recipient_reference"),
        net_amount=result.get("net_amount"),
        fee=result.get("fee"),
        total_charged=result.get("total_charged"),
        new_balance=result.get("new_balance"),
    )


@router.get("/transactions", response_model=list[TransactionOut])
async def list_my_transactions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    rows = (
        await db.execute(
            select(LedgerTransaction)
            .where(LedgerTransaction.user_id == user.id)
            .order_by(LedgerTransaction.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        TransactionOut(
            id=t.id,
            reference=t.reference,
            type=t.type.value,
            provider=t.provider.value,
            amount=Decimal(t.amount),
            fee=Decimal(t.fee),
            status=t.status.value,
            phone_number=t.phone_number,
            proof_ref=t.proof_ref,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in rows
    ]
