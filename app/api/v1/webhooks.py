import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_redis
from app.schemas.schemas import JekoWebhookPayload
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
    x_jeko_signature: str | None = Header(default=None, alias="X-Jeko-Signature"),
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
        transaction = await process_jeko_webhook(db, redis, payload)
        await db.commit()
    except WebhookAlreadyProcessed:
        # Idempotence : on acquitte quand même pour éviter les retries JEKO
        return {"received": True, "status": "already_processed"}
    except TransactionNotFound as exc:
        await db.rollback()
        logger.error("Webhook JEKO pour une transaction inconnue: %s", payload.reference)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction inconnue") from exc

    return {"received": True, "internal_reference": transaction.internal_reference, "status": transaction.status}
