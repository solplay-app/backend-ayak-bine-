"""
Service métier bas niveau pour les transactions et le wallet du CLIENT
FINAL (plus un modèle d'agent — voir models.py pour le contexte du pivot v2).

Responsabilités :
  - Génération de références internes idempotentes
  - Locking distribué (Redis) contre les doubles-soumissions concurrentes
  - Création des transactions en base (PENDING)
  - Crédit/débit du wallet interne, avec verrou pessimiste PostgreSQL
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import MobileOperator, Transaction, TransactionStatus, TransactionType, Wallet

LOCK_TTL_SECONDS = 15


class WalletLockError(Exception):
    """Une opération concurrente est déjà en cours pour cet utilisateur."""


class InsufficientBalanceError(Exception):
    """Le solde wallet est insuffisant pour couvrir le retrait demandé."""


def generate_internal_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


@asynccontextmanager
async def user_redis_lock(redis: Redis, user_id: uuid.UUID) -> AsyncIterator[None]:
    """Verrou distribué Redis (SETNX) empêchant deux opérations simultanées
    (ex: double-tap sur \"Confirmer\") pour le même utilisateur."""
    lock_key = f"user:lock:{user_id}"
    acquired = await redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        raise WalletLockError(f"Opération déjà en cours pour l'utilisateur {user_id}")
    try:
        yield
    finally:
        await redis.delete(lock_key)


async def get_wallet_for_update(db: AsyncSession, user_id: uuid.UUID) -> Wallet:
    """Verrou pessimiste PostgreSQL sur le wallet du client (SELECT ... FOR UPDATE)."""
    stmt = select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one()


async def create_pending_transfer(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    internal_reference: str,
    source_operator: MobileOperator,
    destination_operator: MobileOperator,
    net_amount: Decimal,
    platform_fee: Decimal,
    total_collected: Decimal,
    jeko_deposit_fee_rate: Decimal,
    recipient_name: str,
    recipient_phone: str,
) -> Transaction:
    """
    Crée la transaction TRANSFER en base, status=PENDING, payin_status=PENDING,
    payout_status=None (rempli seulement une fois le pay-in confirmé).
    `internal_reference` est UNIQUE en base : toute tentative de recréation
    avec la même référence lève une IntegrityError (protection double-clic).
    """
    transaction = Transaction(
        internal_reference=internal_reference,
        user_id=user_id,
        type=TransactionType.TRANSFER,
        source_operator=source_operator,
        destination_operator=destination_operator,
        amount=net_amount,
        fee=platform_fee,
        total_collected=total_collected,
        jeko_deposit_fee_rate=jeko_deposit_fee_rate,
        payin_status=TransactionStatus.PENDING,
        payout_status=None,
        status=TransactionStatus.PENDING,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def create_pending_withdrawal(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    internal_reference: str,
    operator: MobileOperator,
    amount: Decimal,
) -> Transaction:
    """
    Crée la transaction WITHDRAWAL en base. Pas d'étape pay-in (l'argent est
    déjà dans le wallet interne) : payin_status est directement SUCCESS,
    seul payout_status suit la progression réelle côté JEKO.
    """
    transaction = Transaction(
        internal_reference=internal_reference,
        user_id=user_id,
        type=TransactionType.WITHDRAWAL,
        source_operator=None,
        destination_operator=operator,
        amount=amount,
        fee=Decimal("0"),
        total_collected=None,
        jeko_deposit_fee_rate=None,
        payin_status=TransactionStatus.SUCCESS,
        payout_status=TransactionStatus.PENDING,
        status=TransactionStatus.PENDING,
        recipient_name=None,
        recipient_phone=None,
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def debit_wallet(db: AsyncSession, wallet: Wallet, amount: Decimal) -> None:
    """Débit optimiste à l'initiation d'un retrait — recrédité si le pay-out échoue ensuite."""
    if Decimal(wallet.balance) < amount:
        raise InsufficientBalanceError(f"Solde insuffisant : {wallet.balance} XOF disponible, {amount} XOF demandé.")
    wallet.balance = Decimal(wallet.balance) - amount


async def credit_wallet(db: AsyncSession, wallet: Wallet, amount: Decimal) -> None:
    wallet.balance = Decimal(wallet.balance) + amount
