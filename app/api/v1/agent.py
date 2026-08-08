import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, verify_pin
from app.database import get_db, get_redis
from app.models.models import Client, Transaction, TransactionStatus, TransactionType, User, Wallet
from app.schemas.schemas import (
    AgentDashboardResponse,
    AgentSettingsUpdate,
    CashInRequest,
    CashInResponse,
    CashOutRequest,
    CashOutResponse,
    ClientRead,
    TransactionRead,
)
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError, get_jeko_client
from app.services.wallet_service import (
    WalletLockError,
    agent_redis_lock,
    create_pending_transaction,
    generate_internal_reference,
    get_or_create_client,
)

router = APIRouter(prefix="/api/v1/agent", tags=["Agent"])

logger = logging.getLogger("agent_api")


def _parse_store_balance(raw: dict) -> tuple[Decimal, str]:
    """
    ATTENTION : la documentation JEKO consultée ne montrait pas d'exemple
    concret du JSON renvoyé par GET /partner_api/stores/{storeId}/balance —
    seulement l'URL de l'endpoint. Cette fonction essaie plusieurs formes
    plausibles (montant en centimes sous "balance"/"amountCents", ou une
    liste "balances": [{"currency":..., "amountCents":...}]) pour éviter de
    planter sur un nom de champ deviné à tort. Si le format réel diffère,
    ça sera visible dans les logs Render (`agent_api`) — à corriger dès que
    le vrai format est confirmé en conditions réelles.
    """
    if "balances" in raw and isinstance(raw["balances"], list) and raw["balances"]:
        entry = raw["balances"][0]
        cents = entry.get("amountCents") or entry.get("balance") or 0
        currency = entry.get("currency", "XOF")
        return Decimal(str(cents)) / 100, currency

    for key in ("amountCents", "balance", "availableCents"):
        if key in raw:
            return Decimal(str(raw[key])) / 100, raw.get("currency", "XOF")

    logger.warning("Format de réponse JEKO balance non reconnu, réponse brute: %s", raw)
    return Decimal("0"), "XOF"


# ---------- Tableau de bord & réglages ----------

@router.get("/dashboard", response_model=AgentDashboardResponse)
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    jeko: JekoClient = Depends(get_jeko_client),
):
    """
    Vue d'ensemble agent : float réel disponible chez JEKO (source de
    vérité, en temps réel) + cumul des commissions gagnées (tracké en
    interne, jamais réinitialisé).
    """
    result = await db.execute(select(Wallet).where(Wallet.user_id == current_user.id))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet de commission introuvable")

    try:
        raw = await jeko.get_store_balance()
        store_balance, currency = _parse_store_balance(raw)
    except (JekoAPIError, JekoNetworkError):
        # Le tableau de bord doit rester utilisable même si JEKO est
        # momentanément indisponible : on affiche le float comme "inconnu"
        # plutôt que de faire planter tout l'écran d'accueil de l'agent.
        store_balance = Decimal("0")
        currency = "XOF"

    return AgentDashboardResponse(
        store_balance=store_balance,
        currency=currency,
        commission_earned_total=wallet.balance,
        commission_rate=current_user.commission_rate,
    )


@router.patch("/settings", status_code=status.HTTP_204_NO_CONTENT)
async def update_settings(
    payload: AgentSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Met à jour le taux de commission appliqué par l'agent (dépôt ET retrait)."""
    current_user.commission_rate = payload.commission_rate
    await db.commit()


# ---------- Fiches clients ----------

@router.get("/clients", response_model=list[ClientRead])
async def list_clients(
    search: str | None = Query(default=None, description="Filtre par nom ou numéro"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Client).where(Client.agent_id == current_user.id).order_by(Client.full_name)
    if search:
        like = f"%{search}%"
        stmt = stmt.where((Client.full_name.ilike(like)) | (Client.phone_number.ilike(like)))
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/clients/{client_id}/transactions", response_model=list[TransactionRead])
async def get_client_transactions(
    client_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Historique des opérations réalisées pour un client donné de cet agent."""
    client_stmt = select(Client).where(Client.id == client_id, Client.agent_id == current_user.id)
    client_result = await db.execute(client_stmt)
    if client_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client introuvable")

    stmt = (
        select(Transaction)
        .where(Transaction.client_id == client_id, Transaction.agent_id == current_user.id)
        .order_by(Transaction.created_at.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


# ---------- Cash-In : le client remet du cash, l'agent crédite son Mobile Money ----------

@router.post("/cash-in", response_model=CashInResponse, status_code=status.HTTP_201_CREATED)
async def cash_in(
    payload: CashInRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    jeko: JekoClient = Depends(get_jeko_client),
    redis: Redis = Depends(get_redis),
):
    """
    Le client a remis du cash à l'agent : l'agent envoie l'équivalent sur le
    Mobile Money du client (JEKO effectue un TRANSFERT depuis le float de
    l'agent). La commission de l'agent est créditée une fois le webhook JEKO
    confirmé (voir webhook_service.process_jeko_webhook).
    """
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    try:
        async with agent_redis_lock(redis, current_user.id):
            client = await get_or_create_client(
                db,
                agent_id=current_user.id,
                phone_number=payload.client_phone_number,
                full_name=payload.client_full_name,
            )

            internal_reference = generate_internal_reference("CIN")
            transaction = await create_pending_transaction(
                db,
                agent_id=current_user.id,
                client_id=client.id,
                internal_reference=internal_reference,
                tx_type=TransactionType.CASH_IN,
                operator=payload.operator,
                amount=payload.amount,
                commission_rate=current_user.commission_rate,
                recipient_phone=payload.client_phone_number,
            )
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Une autre opération est déjà en cours, veuillez patienter.",
        ) from exc

    try:
        jeko_response = await jeko.create_withdrawal_transfer(
            internal_reference=internal_reference,
            amount=payload.amount,
            operator=payload.operator.value,
            phone_number=payload.client_phone_number,
            beneficiary_name=payload.client_full_name,
        )
    except (JekoAPIError, JekoNetworkError) as exc:
        transaction.status = TransactionStatus.FAILED
        await db.commit()
        detail = (
            f"Échec du Cash-In côté JEKO: {exc.payload}"
            if isinstance(exc, JekoAPIError)
            else "Service de paiement momentanément indisponible, réessayez."
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    transaction.jeko_reference = jeko_response.get("id")
    await db.commit()

    return CashInResponse(
        internal_reference=internal_reference,
        status=TransactionStatus.PENDING,
        commission_amount=transaction.commission_amount,
        message="Cash-In initié. Le client va recevoir la confirmation sur son Mobile Money.",
    )


# ---------- Cash-Out : le client paie via Mobile Money, l'agent lui remet du cash ----------

@router.post("/cash-out", response_model=CashOutResponse, status_code=status.HTTP_201_CREATED)
async def cash_out(
    payload: CashOutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    jeko: JekoClient = Depends(get_jeko_client),
    redis: Redis = Depends(get_redis),
):
    """
    Le client veut du cash : son Mobile Money est débité vers le float de
    l'agent (JEKO effectue une COLLECTE/payin depuis le téléphone du
    client), puis l'agent remet le cash physiquement. La commission de
    l'agent est créditée une fois le webhook JEKO confirmé.
    """
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    try:
        async with agent_redis_lock(redis, current_user.id):
            client = await get_or_create_client(
                db,
                agent_id=current_user.id,
                phone_number=payload.client_phone_number,
                full_name=payload.client_full_name,
            )

            internal_reference = generate_internal_reference("COUT")
            transaction = await create_pending_transaction(
                db,
                agent_id=current_user.id,
                client_id=client.id,
                internal_reference=internal_reference,
                tx_type=TransactionType.CASH_OUT,
                operator=payload.operator,
                amount=payload.amount,
                commission_rate=current_user.commission_rate,
                recipient_phone=payload.client_phone_number,
            )
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Une autre opération est déjà en cours, veuillez patienter.",
        ) from exc

    try:
        jeko_response = await jeko.create_deposit_payment_request(
            internal_reference=internal_reference,
            amount=payload.amount,
            operator=payload.operator.value,
            phone_number=payload.client_phone_number,
        )
    except JekoAPIError as exc:
        transaction.status = TransactionStatus.FAILED
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Échec du Cash-Out côté JEKO: {exc.payload}",
        ) from exc
    except JekoNetworkError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Service de paiement momentanément indisponible, réessayez.",
        ) from exc

    transaction.jeko_reference = jeko_response.get("id")
    await db.commit()

    return CashOutResponse(
        internal_reference=internal_reference,
        status=TransactionStatus.PENDING,
        commission_amount=transaction.commission_amount,
        message="Cash-Out initié. Le client doit confirmer le paiement sur son téléphone.",
    )


# ---------- Suivi d'une transaction ----------

@router.get("/transactions/{internal_reference}", response_model=TransactionRead)
async def get_transaction_status(
    internal_reference: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Consultation du statut d'une transaction (polling léger tant que le webhook n'est pas arrivé)."""
    stmt = select(Transaction).where(
        Transaction.internal_reference == internal_reference,
        Transaction.agent_id == current_user.id,
    )
    result = await db.execute(stmt)
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction introuvable")
    return transaction
