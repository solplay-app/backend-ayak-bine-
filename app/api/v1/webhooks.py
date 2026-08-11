import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_redis
from app.models.models import Transaction
from app.schemas.schemas import JekoWebhookPayload
from app.services import kkiapay_client
from app.services.jeko_client import JekoClient, get_jeko_client
from app.services.wallet_service import finalize_kkiapay_deposit
from app.services.webhook_service import (
    TransactionNotFound,
    WebhookAlreadyProcessed,
    process_jeko_webhook,
)
from app.utils.hmac_verify import verify_jeko_signature

logger = logging.getLogger("webhook_endpoint")

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


@router.post("/jeko", status_code=status.HTTP_200_OK)
async def jeko_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    jeko: JekoClient = Depends(get_jeko_client),
    x_jeko_signature: str | None = Header(default=None, alias="Jeko-Signature"),
):
    """
    Réception des notifications JEKO (Pay-In & Pay-Out).

    IMPORTANT :
      - La signature HMAC est calculée sur le corps BRUT (avant tout parsing JSON),
        donc on lit `request.body()` en premier, avant validation Pydantic.
      - On répond toujours 200 dès que le webhook est authentique et traité (même
        si "déjà traité"), pour éviter que JEKO ne le retente indéfiniment.
        Seules la signature invalide et l'absence de transaction renvoient une erreur.
    """
    raw_body = await request.body()

    if not verify_jeko_signature(raw_body, x_jeko_signature):
        logger.warning("Signature webhook JEKO invalide")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Signature invalide")

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Corps JSON invalide") from exc

    try:
        payload = JekoWebhookPayload(**data)
    except Exception as exc:  # pydantic.ValidationError
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    try:
        transaction = await process_jeko_webhook(db, redis, payload, jeko)
        await db.commit()
    except WebhookAlreadyProcessed:
        # Idempotence : on acquitte quand même pour éviter les retries JEKO
        return {"received": True, "status": "already_processed"}
    except TransactionNotFound as exc:
        await db.rollback()
        logger.error("Webhook JEKO pour une référence inconnue: %s", exc)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction inconnue") from exc

    return {"received": True, "internal_reference": transaction.internal_reference, "status": transaction.status}


@router.post("/kkiapay", status_code=status.HTTP_200_OK)
async def kkiapay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_kkiapay_secret: str | None = Header(default=None, alias="x-kkiapay-secret"),
):
    """
    Filet de sécurité en plus de POST /wallet/deposit/confirm (qui reste le
    chemin principal, appelé directement par l'app juste après le widget).
    Utile si l'app crashe ou perd le réseau juste après la fermeture du
    widget Kkiapay, avant d'avoir pu appeler /confirm elle-même.

    On identifie la transaction via `partnerId` — le SDK Flutter Kkiapay
    envoie ce champ dès l'ouverture du widget (voir deposit_screen.dart,
    où on lui passe notre internal_reference), et Kkiapay le renvoie tel
    quel dans le webhook. Contrairement au transactionId Kkiapay (connu
    seulement après coup côté app), partnerId est donc fiable même si
    l'app crashe avant tout appel à /confirm.
    """
    if not kkiapay_client.verify_webhook_secret(x_kkiapay_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Secret webhook invalide")

    raw_body = await request.body()
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Corps JSON invalide") from exc

    kkiapay_transaction_id = data.get("transactionId")
    internal_reference = data.get("partnerId")
    if not kkiapay_transaction_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="transactionId manquant")

    transaction = None
    if internal_reference:
        result = await db.execute(select(Transaction).where(Transaction.internal_reference == internal_reference))
        transaction = result.scalar_one_or_none()
    if transaction is None:
        # Repli : peut-être déjà confirmé via /deposit/confirm, qui a stocké
        # kkiapay_transaction_id dans jeko_payin_id à ce moment-là.
        result = await db.execute(select(Transaction).where(Transaction.jeko_payin_id == kkiapay_transaction_id))
        transaction = result.scalar_one_or_none()
    if transaction is None:
        # On acquitte quand même pour éviter que Kkiapay ne retente indéfiniment.
        return {"received": True, "status": "unmatched_transaction"}

    success = bool(data.get("isPaymentSucces"))
    await finalize_kkiapay_deposit(db, transaction, success=success, kkiapay_transaction_id=kkiapay_transaction_id)
    await db.commit()

    return {"received": True, "internal_reference": transaction.internal_reference, "status": transaction.status}
