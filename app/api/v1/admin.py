"""
Route de réconciliation manuelle utilisable SANS Shell (contournement pour
le plan gratuit Render, où le Shell est désactivé : "Shell is not supported
for free instance types").

Fonctionne en collant simplement une URL dans le navigateur, protégée par un
mot de passe secret (`ADMIN_RECONCILE_SECRET`, à définir dans Render ->
onglet "Environment", PAS besoin de Shell pour ça).

⚠️ Cette route reste un GET (plus simple à utiliser depuis un navigateur)
mais peut déclencher un vrai versement d'argent (--apply). Elle est donc
protégée par un secret long et aléatoire, et n'agit QUE sur une transaction
précise passée en paramètre — jamais en masse.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.services.jeko_client import JekoClient, get_jeko_client
from app.services.reconciliation import (
    TransactionNotFoundForReconciliation,
    reconcile_transaction_by_reference,
)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])
settings = get_settings()


def _check_secret(secret: str) -> None:
    if not settings.admin_reconcile_secret:
        # Route jamais activée si le secret n'a pas été configuré côté Render :
        # évite qu'un déploiement oublié laisse la route ouverte à tout le monde.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Route admin non configurée.")
    if secret != settings.admin_reconcile_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Secret invalide.")


@router.get("/reconcile-transaction")
async def reconcile_transaction(
    secret: str = Query(..., description="Doit correspondre à ADMIN_RECONCILE_SECRET"),
    reference: str = Query(..., description="Référence interne, ex: TRF-ABCD1234"),
    apply: bool = Query(False, description="false = dry-run (rien n'est modifié), true = applique réellement"),
    db: AsyncSession = Depends(get_db),
    jeko: JekoClient = Depends(get_jeko_client),
):
    """
    Vérifie le vrai statut d'une transaction auprès de JEKO et, si `apply=true`,
    rejoue le traitement qu'aurait dû faire le webhook manqué (déclenche le
    pay-out en attente, ou recrédite le wallet si le pay-out a échoué).

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/reconcile-transaction?secret=...&reference=TRF-XXXX
      https://TON-SERVICE.onrender.com/api/v1/admin/reconcile-transaction?secret=...&reference=TRF-XXXX&apply=true
    """
    _check_secret(secret)

    try:
        return await reconcile_transaction_by_reference(db, jeko, reference, apply=apply)
    except TransactionNotFoundForReconciliation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Transaction '{reference}' introuvable.")


@router.get("/pending-transactions")
async def list_pending_transactions(
    secret: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Liste les références des transactions encore PENDING, pour éviter de
    devoir passer par le Shell juste pour les retrouver.

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/pending-transactions?secret=...
    """
    _check_secret(secret)

    from sqlalchemy import select

    from app.models.models import Transaction, TransactionStatus

    result = await db.execute(select(Transaction).where(Transaction.status == TransactionStatus.PENDING))
    transactions = result.scalars().all()
    return [
        {
            "internal_reference": t.internal_reference,
            "type": t.type.value,
            "amount": str(t.amount),
            "payin_status": t.payin_status.value if t.payin_status else None,
            "payout_status": t.payout_status.value if t.payout_status else None,
            "created_at": t.created_at.isoformat(),
        }
        for t in transactions
    ]
