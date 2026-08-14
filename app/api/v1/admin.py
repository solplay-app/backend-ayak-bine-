"""
Pages admin utilisables SANS Shell (le plan gratuit Render désactive le
Shell) : consultation et validation manuelle, via de simples URL protégées
par un secret, collées dans le navigateur.

Protégé par ADMIN_BOOTSTRAP_SECRET (déjà défini sur Render pour la création
du premier compte admin — réutilisé ici pour éviter une variable de plus).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import KycStatus, KycSubmission, LedgerTransaction, TransactionStatus, User, Wallet

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])
settings = get_settings()


def _check_secret(secret: str) -> None:
    if not settings.ADMIN_BOOTSTRAP_SECRET:
        # Route jamais activée si le secret n'a pas été configuré côté Render :
        # évite qu'un déploiement oublié laisse la route ouverte à tout le monde.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Route admin non configurée.")
    if secret != settings.ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Secret invalide.")


# ---------------------------- Transactions -------------------------------

@router.get("/pending-transactions")
async def list_pending_transactions(
    secret: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Liste les transactions encore PENDING (pay-in déclarés en attente de
    validation, pay-out en attente de traitement), pour éviter de devoir
    passer par le Shell juste pour les retrouver.

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/pending-transactions?secret=...
    """
    _check_secret(secret)

    result = await db.execute(
        select(LedgerTransaction)
        .where(LedgerTransaction.status == TransactionStatus.PENDING)
        .order_by(LedgerTransaction.created_at.asc())
    )
    transactions = result.scalars().all()
    return [
        {
            "reference": t.reference,
            "type": t.type.value,
            "provider": t.provider.value,
            "amount": str(t.amount),
            "fee": str(t.fee),
            "phone_number": t.phone_number,
            "proof_ref": t.proof_ref,
            "created_at": t.created_at.isoformat(),
        }
        for t in transactions
    ]


@router.get("/user-history")
async def user_transaction_history(
    secret: str = Query(...),
    reference: str = Query(..., description="N'importe quelle référence interne d'une transaction de ce client, ex: PI-ABCD1234"),
    db: AsyncSession = Depends(get_db),
):
    """
    Retrouve le client propriétaire d'une transaction donnée, puis liste
    tout son historique (avec le solde wallet actuel).

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/user-history?secret=...&reference=PI-XXXX
    """
    _check_secret(secret)

    anchor = (
        await db.execute(select(LedgerTransaction).where(LedgerTransaction.reference == reference))
    ).scalar_one_or_none()
    if anchor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Transaction '{reference}' introuvable.")

    user = (await db.execute(select(User).where(User.id == anchor.user_id))).scalar_one_or_none()
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == anchor.user_id))).scalar_one_or_none()

    history = (
        await db.execute(
            select(LedgerTransaction)
            .where(LedgerTransaction.user_id == anchor.user_id)
            .order_by(LedgerTransaction.created_at.asc())
        )
    ).scalars().all()

    return {
        "user_id": str(anchor.user_id),
        "user_phone": user.phone_number if user else None,
        "current_wallet_balance": str(wallet.balance) if wallet else None,
        "transaction_count": len(history),
        "history": [
            {
                "reference": t.reference,
                "type": t.type.value,
                "provider": t.provider.value,
                "amount": str(t.amount),
                "fee": str(t.fee),
                "status": t.status.value,
                "phone_number": t.phone_number,
                "created_at": t.created_at.isoformat(),
            }
            for t in history
        ],
    }


# ---------- Vérification d'identité (KYC) — validation manuelle ----------

@router.get("/kyc/pending")
async def kyc_pending_page(secret: str = Query(...), db: AsyncSession = Depends(get_db)):
    """
    Page HTML (pas du JSON brut, exprès) listant les demandes KYC en attente,
    avec les photos affichées directement. Chaque demande a un bouton
    Valider / Rejeter.

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/kyc/pending?secret=...
    """
    _check_secret(secret)

    result = await db.execute(
        select(KycSubmission, User)
        .join(User, User.id == KycSubmission.user_id)
        .where(KycSubmission.status == KycStatus.PENDING)
        .order_by(KycSubmission.created_at.asc())
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
              <p style="color:#888;margin:0 0 12px 0;font-size:13px;">Demande envoyée le {submission.created_at.strftime('%d/%m/%Y à %H:%M')}</p>
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
    directement). Idempotent : si la demande n'est plus PENDING (déjà
    traitée), ne fait rien.
    """
    _check_secret(secret)

    submission = (
        await db.execute(select(KycSubmission).where(KycSubmission.id == submission_id))
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demande introuvable.")

    if submission.status != KycStatus.PENDING:
        return HTMLResponse(f"<p>Cette demande a déjà été traitée (statut actuel : {submission.status.value}).</p>")

    user = (await db.execute(select(User).where(User.id == submission.user_id))).scalar_one()

    if decision == "approve":
        submission.status = KycStatus.APPROVED
        submission.review_note = None
        message = f"✅ Compte de {user.full_name} vérifié avec succès."
    else:
        submission.status = KycStatus.REJECTED
        submission.review_note = reason or "Document non conforme"
        message = f"❌ Demande de {user.full_name} rejetée ({submission.review_note}). Le client peut renvoyer de nouveaux documents."

    submission.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return HTMLResponse(f"<p>{message}</p><p><a href='/api/v1/admin/kyc/pending?secret={secret}'>← Retour aux demandes en attente</a></p>")
