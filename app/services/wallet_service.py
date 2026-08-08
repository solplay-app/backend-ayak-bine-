"""
Service métier pour les opérations d'agent (Cash-In / Cash-Out).

Important changement de modèle : ce système ne détient PLUS les fonds des
clients en interne. L'agent opère avec son propre "float" hébergé chez JEKO
(visible via JekoClient.get_store_balance()) — le `Wallet` local ne sert
qu'à comptabiliser les COMMISSIONS gagnées par l'agent au fil du temps.

Responsabilités restantes ici :
  - Génération de références internes idempotentes
  - Locking distribué (Redis) pour éviter les doubles-soumissions
    concurrentes d'une même opération par le même agent
  - Crédit du wallet de commission (jamais de débit lié à un solde client,
    puisqu'aucun solde client n'est détenu ici)
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Client, Transaction, TransactionStatus, Wallet

LOCK_TTL_SECONDS = 15


class WalletLockError(Exception):
    """Une opération concurrente est déjà en cours pour cet agent."""


def generate_internal_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


@asynccontextmanager
async def agent_redis_lock(redis: Redis, agent_id: uuid.UUID) -> AsyncIterator[None]:
    """
    Verrou distribué Redis (SETNX) empêchant deux opérations simultanées
    (ex: deux Cash-Out lancés en double-tap) pour le même agent.
    """
    lock_key = f"agent:lock:{agent_id}"
    acquired = await redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        raise WalletLockError(f"Opération déjà en cours pour l'agent {agent_id}")
    try:
        yield
    finally:
        await redis.delete(lock_key)


async def get_or_create_client(
    db: AsyncSession, *, agent_id: uuid.UUID, phone_number: str, full_name: str
) -> Client:
    """
    Récupère la fiche client existante de cet agent pour ce numéro, ou en
    crée une nouvelle. Si le nom fourni diffère de celui enregistré, on met
    à jour (l'agent a pu se tromper la première fois ou le client a
    communiqué son nom correct).
    """
    stmt = select(Client).where(Client.agent_id == agent_id, Client.phone_number == phone_number)
    result = await db.execute(stmt)
    client = result.scalar_one_or_none()

    if client is None:
        client = Client(agent_id=agent_id, phone_number=phone_number, full_name=full_name)
        db.add(client)
        await db.flush()
    elif client.full_name != full_name:
        client.full_name = full_name

    return client


async def get_wallet_for_update(db: AsyncSession, agent_id: uuid.UUID) -> Wallet:
    """Verrou pessimiste PostgreSQL sur le wallet de commission de l'agent."""
    stmt = select(Wallet).where(Wallet.user_id == agent_id).with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one()


async def create_pending_transaction(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    client_id: uuid.UUID,
    internal_reference: str,
    tx_type,
    operator,
    amount: Decimal,
    commission_rate: Decimal,
    fee: Decimal = Decimal("0"),
    recipient_phone: str | None = None,
    metadata: dict | None = None,
) -> Transaction:
    """
    Crée la transaction en base avec status=PENDING. `internal_reference` est
    UNIQUE en base : toute tentative de recréation avec la même référence
    lève une IntegrityError (protection contre le double-clic).
    """
    commission_amount = (amount * commission_rate).quantize(Decimal("1"))
    transaction = Transaction(
        internal_reference=internal_reference,
        agent_id=agent_id,
        client_id=client_id,
        type=tx_type,
        operator=operator,
        amount=amount,
        fee=fee,
        commission_amount=commission_amount,
        commission_rate=commission_rate,
        status=TransactionStatus.PENDING,
        recipient_phone=recipient_phone,
        metadata_=metadata,
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def credit_commission(db: AsyncSession, wallet: Wallet, commission_amount: Decimal) -> None:
    """Crédite le wallet de commission de l'agent - appelé uniquement quand
    le webhook JEKO confirme le succès réel de l'opération."""
    wallet.balance += commission_amount
