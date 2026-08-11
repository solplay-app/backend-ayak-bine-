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
from fastapi.responses import HTMLResponse
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


@router.get("/user-history")
async def user_transaction_history(
    secret: str = Query(...),
    reference: str = Query(..., description="N'importe quelle référence interne d'une transaction de ce client, ex: TRF-FE7064473D0B44F2"),
    db: AsyncSession = Depends(get_db),
):
    """
    Retrouve le client propriétaire d'une transaction donnée, puis liste
    TOUT son historique (avec le solde wallet actuel), pour vérifier
    précisément d'où vient un solde donné (utile après une réconciliation
    manuelle, pour confirmer qu'un montant recrédité correspond bien à ce
    qui était attendu).

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/user-history?secret=...&reference=TRF-XXXX
    """
    _check_secret(secret)

    from sqlalchemy import select

    from app.models.models import Transaction, Wallet

    result = await db.execute(select(Transaction).where(Transaction.internal_reference == reference))
    anchor_transaction = result.scalar_one_or_none()
    if anchor_transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Transaction '{reference}' introuvable.")

    user_id = anchor_transaction.user_id

    wallet_result = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    wallet = wallet_result.scalar_one_or_none()

    history_result = await db.execute(
        select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.created_at.asc())
    )
    transactions = history_result.scalars().all()

    return {
        "user_id": str(user_id),
        "current_wallet_balance": str(wallet.balance) if wallet else None,
        "transaction_count": len(transactions),
        "history": [
            {
                "internal_reference": t.internal_reference,
                "type": t.type.value,
                "amount": str(t.amount),
                "fee": str(t.fee),
                "total_collected": str(t.total_collected) if t.total_collected is not None else None,
                "status": t.status.value,
                "payin_status": t.payin_status.value if t.payin_status else None,
                "payout_status": t.payout_status.value if t.payout_status else None,
                "recipient_phone": t.recipient_phone,
                "created_at": t.created_at.isoformat(),
            }
            for t in transactions
        ],
    }

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


# ---------- Vérification d'identité (KYC) — validation manuelle ----------

@router.get("/kyc/pending")
async def kyc_pending_page(secret: str = Query(...), db: AsyncSession = Depends(get_db)):
    """
    Page HTML (pas du JSON brut, exprès) listant les demandes KYC en attente,
    avec les photos affichées directement — pour pouvoir les regarder et
    décider sans outil externe. Chaque demande a un bouton Valider / Rejeter.

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/kyc/pending?secret=...
    """
    _check_secret(secret)

    from sqlalchemy import select

    from app.models.models import KycSubmission, User

    result = await db.execute(
        select(KycSubmission, User)
        .join(User, User.id == KycSubmission.user_id)
        .where(KycSubmission.status == "UNDER_REVIEW")
        .order_by(KycSubmission.submitted_at.asc())
    )
    rows = result.all()

    if not rows:
        body = "<p>Aucune demande en attente. ✅</p>"
    else:
        cards = []
        for submission, user in rows:
            selfie_html = (
                f'<img src="data:image/jpeg;base64,{submission.selfie_base64}" style="max-width:280px;border-radius:8px;margin-left:12px;" />'
                if submission.selfie_base64
                else "<p style='color:#888'>(pas de selfie envoyé)</p>"
            )
            cards.append(f"""
            <div style="border:1px solid #ddd;border-radius:12px;padding:16px;margin-bottom:24px;">
              <h3 style="margin:0 0 4px 0;">{user.full_name} — {user.phone_number}</h3>
              <p style="color:#888;margin:0 0 12px 0;font-size:13px;">Demande envoyée le {submission.submitted_at.strftime('%d/%m/%Y à %H:%M')}</p>
              <div style="display:flex;flex-wrap:wrap;">
                <img src="data:image/jpeg;base64,{submission.id_document_base64}" style="max-width:280px;border-radius:8px;" />
                {selfie_html}
              </div>
              <div style="margin-top:14px;">
                <a href="/api/v1/admin/kyc/decide?secret={secret}&submission_id={submission.id}&decision=approve"
                   style="background:#16a34a;color:white;padding:10px 18px;border-radius:8px;text-decoration:none;margin-right:10px;">✅ Valider</a>
                <a href="/api/v1/admin/kyc/decide?secret={secret}&submission_id={submission.id}&decision=reject&reason=Document%20illisible%20ou%20invalide"
                   style="background:#dc2626;color:white;padding:10px 18px;border-radius:8px;text-decoration:none;">❌ Rejeter (document illisible)</a>
              </div>
            </div>
            """)
        body = "".join(cards)

    html = f"""
    <html><head><meta charset="utf-8"><title>Demandes KYC en attente</title></head>
    <body style="font-family:sans-serif;max-width:800px;margin:24px auto;padding:0 16px;">
      <h2>Demandes de vérification d'identité en attente</h2>
      {body}
    </body></html>
    """
    return HTMLResponse(content=html)


@router.get("/kyc/decide")
async def kyc_decide(
    secret: str = Query(...),
    submission_id: str = Query(...),
    decision: str = Query(..., pattern="^(approve|reject)$"),
    reason: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Applique la décision (clic depuis /admin/kyc/pending, ou URL collée
    directement). Idempotent : si la demande n'est plus UNDER_REVIEW
    (déjà traitée), ne fait rien.
    """
    _check_secret(secret)

    from sqlalchemy import select

    from app.models.models import KycStatus, KycSubmission, User

    result = await db.execute(select(KycSubmission).where(KycSubmission.id == submission_id))
    submission = result.scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demande introuvable.")

    if submission.status != "UNDER_REVIEW":
        return HTMLResponse(f"<p>Cette demande a déjà été traitée (statut actuel : {submission.status}).</p>")

    user_result = await db.execute(select(User).where(User.id == submission.user_id))
    user = user_result.scalar_one()

    from datetime import datetime, timezone

    if decision == "approve":
        submission.status = "VERIFIED"
        user.kyc_status = KycStatus.VERIFIED
        message = f"✅ Compte de {user.full_name} vérifié avec succès."
    else:
        submission.status = "REJECTED"
        submission.rejection_reason = reason or "Document non conforme"
        user.kyc_status = KycStatus.REJECTED
        message = f"❌ Demande de {user.full_name} rejetée ({submission.rejection_reason}). Le client peut renvoyer de nouveaux documents."

    submission.reviewed_at = datetime.now(timezone.utc)
    await db.commit()

    return HTMLResponse(f"<p>{message}</p><p><a href='/api/v1/admin/kyc/pending?secret={secret}'>← Retour aux demandes en attente</a></p>")
