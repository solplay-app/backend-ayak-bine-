"""
Pages admin utilisables SANS Shell (le plan gratuit Render désactive le
Shell) : consultation et validation manuelle, via de simples URL protégées
par un secret, collées dans le navigateur.

Protégé par ADMIN_BOOTSTRAP_SECRET (déjà défini sur Render pour la création
du premier compte admin — réutilisé ici pour éviter une variable de plus).
"""
from __future__ import annotations

from datetime import datetime, timezone

from datetime import datetime as _dt, time as _time
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import cast, func, select, text
from sqlalchemy import String as SAString
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db, get_sync_db
from app.models import (
    KycStatus, KycSubmission, LedgerTransaction, TransactionStatus,
    TransactionType, User, UserRole, Wallet,
)
from app.schemas import (
    AdminActionRequest, AdminCreditRequest, AdminUserOut, DailyStatPoint, DashboardStatsResponse,
    FeePercentResponse, FeePercentUpdateRequest, KycDecisionRequest, PaymentLinksResponse,
    PaymentLinksUpdateRequest, UserStatusUpdateRequest,
)
from app.push_service import notify_user_background
from app.wallet_service import (
    admin_manual_credit, admin_process_payout, finalize_payin,
    get_fee_percent, get_payment_links, set_fee_percent, set_payment_links,
)

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])
settings = get_settings()


def _check_secret(secret: str) -> None:
    if not settings.ADMIN_BOOTSTRAP_SECRET:
        # Route jamais activée si le secret n'a pas été configuré côté Render :
        # évite qu'un déploiement oublié laisse la route ouverte à tout le monde.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Route admin non configurée.")
    if secret != settings.ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Secret invalide.")


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Dépendance JWT — réutilise la connexion normale (/auth/login), exige role=ADMIN."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Réservé aux administrateurs.")
    return user


# ------------------------- Tableau de bord (JWT) --------------------------

@router.get("/dashboard-stats", response_model=DashboardStatsResponse)
async def dashboard_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Vue d'ensemble pour le dashboard : soldes, flux du jour, frais collectés."""
    today_start = _dt.combine(_dt.now(tz=None).date(), _time.min)

    solde_global = (await db.execute(select(func.coalesce(func.sum(Wallet.balance), 0)))).scalar_one()

    recu_jour = (await db.execute(
        select(func.coalesce(func.sum(LedgerTransaction.amount), 0)).where(
            LedgerTransaction.type == TransactionType.PAY_IN,
            LedgerTransaction.status == TransactionStatus.SUCCESS,
            LedgerTransaction.created_at >= today_start,
        )
    )).scalar_one()

    retire_jour = (await db.execute(
        select(func.coalesce(func.sum(LedgerTransaction.amount), 0)).where(
            LedgerTransaction.type == TransactionType.PAY_OUT,
            LedgerTransaction.status == TransactionStatus.SUCCESS,
            LedgerTransaction.created_at >= today_start,
        )
    )).scalar_one()

    frais_jour = (await db.execute(
        select(func.coalesce(func.sum(LedgerTransaction.fee), 0)).where(
            LedgerTransaction.created_at >= today_start,
        )
    )).scalar_one()

    frais_total = (await db.execute(
        select(func.coalesce(func.sum(LedgerTransaction.fee), 0))
    )).scalar_one()

    nb_users = (await db.execute(select(func.count(User.id)))).scalar_one()

    nb_pending_payouts = (await db.execute(
        select(func.count(LedgerTransaction.id)).where(
            LedgerTransaction.type == TransactionType.PAY_OUT,
            LedgerTransaction.status == TransactionStatus.PENDING,
        )
    )).scalar_one()

    # Lecture directe du réglage (table simple key/value, pas de modèle ORM dédié).
    from sqlalchemy import text as _text
    fp_row = (await db.execute(_text("SELECT value FROM platform_settings WHERE key = 'fee_percent'"))).first()
    fee_percent = Decimal(fp_row[0]) if fp_row else Decimal("8")

    return DashboardStatsResponse(
        solde_global=Decimal(solde_global),
        recu_aujourdhui=Decimal(recu_jour),
        retire_aujourdhui=Decimal(retire_jour),
        frais_collectes_aujourdhui=Decimal(frais_jour),
        frais_collectes_total=Decimal(frais_total),
        fee_percent=fee_percent,
        nb_utilisateurs=nb_users,
        nb_payouts_en_attente=nb_pending_payouts,
    )


@router.get("/settings/fee-percent", response_model=FeePercentResponse)
async def get_fee_percent_route(
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    return FeePercentResponse(fee_percent=get_fee_percent(db))


@router.put("/settings/fee-percent", response_model=FeePercentResponse)
async def update_fee_percent_route(
    payload: FeePercentUpdateRequest,
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    set_fee_percent(db, payload.fee_percent)
    return FeePercentResponse(fee_percent=payload.fee_percent)


@router.get("/settings/payment-links", response_model=PaymentLinksResponse)
async def get_payment_links_route(
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    return PaymentLinksResponse(**get_payment_links(db))


@router.put("/settings/payment-links", response_model=PaymentLinksResponse)
async def update_payment_links_route(
    payload: PaymentLinksUpdateRequest,
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    set_payment_links(db, payload.model_dump())
    return PaymentLinksResponse(**get_payment_links(db))


# ------------------------- Pay-Out en attente (JWT) ------------------------

@router.get("/pending-payouts")
async def pending_payouts(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(LedgerTransaction, User.phone_number.label("user_phone"))
        .join(User, User.id == LedgerTransaction.user_id)
        .where(
            LedgerTransaction.type == TransactionType.PAY_OUT,
            LedgerTransaction.status == TransactionStatus.PENDING,
        )
        .order_by(LedgerTransaction.created_at.asc())
    )
    rows = result.all()
    return [
        {
            "id": str(t.id),
            "reference": t.reference,
            "provider": t.provider.value,
            "amount": str(t.amount),
            "phone_number": t.phone_number,
            "user_phone": user_phone,
            "created_at": t.created_at.isoformat(),
        }
        for t, user_phone in rows
    ]


@router.post("/process-payout/{transaction_id}")
async def process_payout_route(
    transaction_id: str,
    payload: AdminActionRequest,
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    if payload.action.upper() not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=400, detail="Action invalide (APPROVE ou REJECT)")
    result = admin_process_payout(
        db,
        transaction_id=transaction_id,
        action=payload.action.upper(),
        proof_ref=payload.proof_ref,
        admin_id=admin.id,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Échec"))
    return result


# ------------------------- Historique transactions (JWT) -------------------

@router.get("/transactions")
async def list_transactions(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    type: str | None = Query(default=None, description="PAY_IN, PAY_OUT ou INTERNAL_TRANSFER"),
    status_filter: str | None = Query(default=None, alias="status"),
    phone: str | None = Query(default=None, description="Recherche par numéro (client ou destinataire)"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Liste paginée et filtrable de toutes les transactions — vue admin complète."""
    q = select(LedgerTransaction, User.phone_number.label("user_phone")).join(
        User, User.id == LedgerTransaction.user_id
    )
    if type:
        try:
            q = q.where(LedgerTransaction.type == TransactionType(type.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Type de transaction invalide.")
    if status_filter:
        try:
            q = q.where(LedgerTransaction.status == TransactionStatus(status_filter.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail="Statut invalide.")
    if phone:
        like = f"%{phone.strip()}%"
        q = q.where((User.phone_number.ilike(like)) | (LedgerTransaction.phone_number.ilike(like)))

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    q = q.order_by(LedgerTransaction.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(q)).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": str(t.id),
                "reference": t.reference,
                "type": t.type.value,
                "provider": t.provider.value,
                "amount": str(t.amount),
                "fee": str(t.fee),
                "status": t.status.value,
                "phone_number": t.phone_number,
                "user_phone": user_phone,
                "proof_ref": t.proof_ref,
                "created_at": t.created_at.isoformat(),
            }
            for t, user_phone in rows
        ],
    }


# ------------------------- Statistiques journalières (JWT) -----------------

@router.get("/stats/daily", response_model=list[DailyStatPoint])
async def daily_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=14, le=90, ge=1),
):
    """Série temporelle (reçu / retiré / frais par jour) pour les graphiques du dashboard."""
    from sqlalchemy import text as _text

    rows = (
        await db.execute(
            _text("""
                SELECT
                    d::date AS day,
                    COALESCE(SUM(amount) FILTER (WHERE type = 'PAY_IN' AND status = 'SUCCESS'), 0) AS recu,
                    COALESCE(SUM(amount) FILTER (WHERE type = 'PAY_OUT' AND status = 'SUCCESS'), 0) AS retire,
                    COALESCE(SUM(fee), 0) AS frais
                FROM generate_series(
                    CURRENT_DATE - (:days - 1) * INTERVAL '1 day', CURRENT_DATE, INTERVAL '1 day'
                ) AS d
                LEFT JOIN ledger_transactions
                    ON ledger_transactions.created_at::date = d::date
                GROUP BY d
                ORDER BY d ASC
            """),
            {"days": days},
        )
    ).all()

    return [
        DailyStatPoint(date=str(r.day), recu=Decimal(r.recu), retire=Decimal(r.retire), frais=Decimal(r.frais))
        for r in rows
    ]


# ------------------------- Gestion des utilisateurs (JWT) ------------------

@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    search: str | None = Query(default=None, description="Recherche par téléphone ou nom"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = select(User, Wallet.balance).join(Wallet, Wallet.user_id == User.id, isouter=True)
    if search:
        like = f"%{search.strip()}%"
        q = q.where((User.phone_number.ilike(like)) | (User.full_name.ilike(like)))
    q = q.order_by(User.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(q)).all()

    # Statut KYC le plus récent par utilisateur (une seule requête groupée).
    user_ids = [u.id for u, _ in rows]
    kyc_map: dict = {}
    if user_ids:
        # cast en texte : évite un crash SQLAlchemy si une ligne contient une
        # valeur de statut qui n'existe plus dans l'enum Python KycStatus
        # (ex. anciennes valeurs "VERIFIED" jamais nettoyées en base).
        kyc_rows = (
            await db.execute(
                select(KycSubmission.user_id, cast(KycSubmission.status, SAString))
                .where(KycSubmission.user_id.in_(user_ids))
                .order_by(KycSubmission.created_at.desc())
            )
        ).all()
        for uid, kstatus in kyc_rows:
            kyc_map.setdefault(uid, kstatus)

    return [
        AdminUserOut(
            id=str(u.id),
            phone_number=u.phone_number,
            full_name=u.full_name,
            role=u.role.value,
            is_active=u.is_active,
            kyc_status=kyc_map.get(u.id),
            wallet_balance=Decimal(balance or 0),
            created_at=u.created_at,
        )
        for u, balance in rows
    ]


@router.get("/users/{user_id}")
async def user_detail(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one_or_none()
    history = (
        await db.execute(
            select(LedgerTransaction)
            .where(LedgerTransaction.user_id == user.id)
            .order_by(LedgerTransaction.created_at.desc())
            .limit(100)
        )
    ).scalars().all()

    return {
        "id": str(user.id),
        "phone_number": user.phone_number,
        "full_name": user.full_name,
        "role": user.role.value,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "wallet_balance": str(wallet.balance) if wallet else "0",
        "history": [
            {
                "reference": t.reference,
                "type": t.type.value,
                "provider": t.provider.value,
                "amount": str(t.amount),
                "fee": str(t.fee),
                "status": t.status.value,
                "created_at": t.created_at.isoformat(),
            }
            for t in history
        ],
    }


@router.put("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    payload: UserStatusUpdateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Bloque / débloque un compte client (ex: comportement suspect)."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    if user.role == UserRole.ADMIN:
        raise HTTPException(status_code=400, detail="Impossible de bloquer un compte administrateur.")
    user.is_active = payload.is_active
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True, "id": str(user.id), "is_active": user.is_active}


@router.post("/users/{user_id}/credit")
async def credit_user_wallet(
    user_id: str,
    payload: AdminCreditRequest,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    """Recharge manuelle du solde d'un client par l'admin — dépannage,
    remboursement commercial, ou avance en attendant une vraie intégration
    opérateur (Wave/Orange). Aucun paiement réel n'est vérifié ici : à
    utiliser uniquement quand l'argent a déjà été reçu par un autre moyen.
    """
    result = admin_manual_credit(
        db, user_id=user_id, amount=payload.amount, reason=payload.reason, admin_id=admin.id,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Échec de la recharge."))
    # En tâche de fond : l'appel réseau Google/FCM est synchrone et peut être
    # lent, il ne doit jamais retarder ni faire échouer la réponse à l'admin.
    background_tasks.add_task(
        notify_user_background, user_id,
        title="Solde rechargé",
        body=f"{payload.amount} XOF ont été ajoutés à votre solde Ayak'bine.",
        data={"type": "wallet_credited", "reference": result.get("reference", "")},
    )
    return result


# ---------- Vérification d'identité (KYC) — via JWT, pour le dashboard -----

@router.get("/kyc/pending-list")
async def kyc_pending_list(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Version JSON (consommée par le dashboard) de /kyc/pending."""
    result = await db.execute(
        select(KycSubmission, User)
        .join(User, User.id == KycSubmission.user_id)
        .where(KycSubmission.status == KycStatus.PENDING)
        .order_by(KycSubmission.created_at.asc())
    )
    rows = result.all()
    return [
        {
            "submission_id": str(submission.id),
            "user_phone": user.phone_number,
            "user_full_name": user.full_name,
            "id_document_base64": submission.id_document_base64,
            "selfie_base64": submission.selfie_base64,
            "created_at": submission.created_at.isoformat(),
        }
        for submission, user in rows
    ]


@router.post("/kyc/{submission_id}/decide")
async def kyc_decide_jwt(
    submission_id: str,
    payload: KycDecisionRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    submission = (
        await db.execute(select(KycSubmission).where(KycSubmission.id == submission_id))
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    if submission.status != KycStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Déjà traitée (statut actuel : {submission.status.value}).")

    user = (await db.execute(select(User).where(User.id == submission.user_id))).scalar_one()

    if payload.decision == "approve":
        submission.status = KycStatus.APPROVED
        submission.review_note = None
        message = f"Compte de {user.full_name} vérifié avec succès."
    else:
        submission.status = KycStatus.REJECTED
        submission.review_note = payload.reason or "Document non conforme"
        message = f"Demande de {user.full_name} rejetée."

    submission.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True, "message": message}


# ------------------------- Dépôts (Pay-In) en attente (JWT) ----------------
# Filet de sécurité : le crédit est normalement automatique (webhook/SMS
# opérateur). Si ça échoue ou n'est pas encore branché, le dépôt reste
# PENDING indéfiniment sans ceci — l'admin doit pouvoir intervenir.

@router.get("/pending-payins")
async def pending_payins(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(LedgerTransaction, User.phone_number.label("user_phone"))
        .join(User, User.id == LedgerTransaction.user_id)
        .where(
            LedgerTransaction.type == TransactionType.PAY_IN,
            LedgerTransaction.status == TransactionStatus.PENDING,
        )
        .order_by(LedgerTransaction.created_at.asc())
    )
    rows = result.all()
    return [
        {
            "id": str(t.id),
            "reference": t.reference,
            "provider": t.provider.value,
            "amount": str(t.amount),
            "phone_number": t.phone_number,
            "user_phone": user_phone,
            "created_at": t.created_at.isoformat(),
        }
        for t, user_phone in rows
    ]


@router.post("/process-payin/{transaction_id}")
async def process_payin_route(
    transaction_id: str,
    payload: AdminActionRequest,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Annotated[Session, Depends(get_sync_db)] = None,
):
    """Valide ou rejette manuellement un dépôt resté bloqué en PENDING
    (webhook/SMS jamais reçu, ou intégration opérateur pas encore active).
    """
    if payload.action.upper() not in ("APPROVE", "REJECT"):
        raise HTTPException(status_code=400, detail="Action invalide (APPROVE ou REJECT)")

    if payload.action.upper() == "REJECT":
        row = db.execute(
            text("""
                UPDATE ledger_transactions
                SET status = 'FAILED', updated_at = CURRENT_TIMESTAMP
                WHERE id = :tid AND status = 'PENDING' AND type = 'PAY_IN'
                RETURNING reference
            """),
            {"tid": transaction_id},
        ).first()
        db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Dépôt introuvable ou déjà traité.")
        return {"success": True, "message": f"Dépôt {row[0]} rejeté."}

    tx_row = db.execute(
        text("SELECT reference, amount, user_id FROM ledger_transactions WHERE id = :tid AND type = 'PAY_IN'"),
        {"tid": transaction_id},
    ).first()
    if not tx_row:
        raise HTTPException(status_code=404, detail="Dépôt introuvable.")

    result = finalize_payin(
        db,
        reference=tx_row.reference,
        proof_ref=payload.proof_ref or f"MANUEL-ADMIN-{admin.phone_number}",
        confirmed_amount=tx_row.amount,  # confirmation manuelle : on fait confiance au montant déclaré
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Échec de la validation."))
    # En tâche de fond, pour la même raison que credit_user_wallet ci-dessus.
    background_tasks.add_task(
        notify_user_background, tx_row.user_id,
        title="Wallet rechargé",
        body=f"{tx_row.amount} XOF ont été ajoutés à votre solde Ayak'bine.",
        data={"type": "wallet_credited", "reference": tx_row.reference},
    )
    return result


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


# ---------- Liste des utilisateurs — page HTML (secret) -------------------

@router.get("/users-page")
async def users_html_page(
    secret: str = Query(...),
    search: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Page HTML listant tous les utilisateurs (nom, téléphone, solde, statut
    KYC, actif/inactif), dans le même esprit que /kyc/pending : pas besoin
    du dashboard JWT, une URL + le secret admin suffisent.

    Utilisation (coller dans le navigateur) :
      https://TON-SERVICE.onrender.com/api/v1/admin/users-page?secret=...
    """
    _check_secret(secret)

    q = select(User, Wallet.balance).join(Wallet, Wallet.user_id == User.id, isouter=True)
    if search:
        like = f"%{search.strip()}%"
        q = q.where((User.phone_number.ilike(like)) | (User.full_name.ilike(like)))
    q = q.order_by(User.created_at.desc()).limit(500)
    rows = (await db.execute(q)).all()

    user_ids = [u.id for u, _ in rows]
    kyc_map: dict = {}
    if user_ids:
        kyc_rows = (
            await db.execute(
                select(KycSubmission.user_id, cast(KycSubmission.status, SAString))
                .where(KycSubmission.user_id.in_(user_ids))
                .order_by(KycSubmission.created_at.desc())
            )
        ).all()
        for uid, kstatus in kyc_rows:
            kyc_map.setdefault(uid, kstatus)

    kyc_badge = {
        "APPROVED": "background:#16a34a;color:white;",
        "PENDING": "background:#eab308;color:#1a1a1a;",
        "REJECTED": "background:#dc2626;color:white;",
    }

    if not rows:
        table_rows = '<tr><td colspan="6" style="padding:16px;text-align:center;color:#888;">Aucun utilisateur trouvé.</td></tr>'
    else:
        parts = []
        for u, balance in rows:
            kstatus = kyc_map.get(u.id, "NON SOUMIS")
            badge_style = kyc_badge.get(kstatus, "background:#e5e7eb;color:#444;")
            status_label = "🟢 Actif" if u.is_active else "🔴 Désactivé"
            parts.append(f"""
            <tr style="border-bottom:1px solid #eee;">
              <td style="padding:10px;">{u.full_name}</td>
              <td style="padding:10px;">{u.phone_number}</td>
              <td style="padding:10px;">{u.role.value}</td>
              <td style="padding:10px;">{Decimal(balance or 0):,.0f} XOF</td>
              <td style="padding:10px;"><span style="{badge_style}padding:4px 10px;border-radius:999px;font-size:12px;">{kstatus}</span></td>
              <td style="padding:10px;">{status_label}</td>
            </tr>
            """)
        table_rows = "".join(parts)

    html = f"""
    <html><head><meta charset="utf-8"><title>Utilisateurs — Ayak'bine</title></head>
    <body style="font-family:sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;">
      <h2>Liste des utilisateurs ({len(rows)})</h2>
      <p><a href="/api/v1/admin/kyc/pending?secret={secret}">→ Voir les demandes KYC en attente</a></p>
      <form method="get" style="margin-bottom:16px;">
        <input type="hidden" name="secret" value="{secret}" />
        <input type="text" name="search" placeholder="Rechercher (nom ou téléphone)" value="{search or ''}"
               style="padding:8px;border:1px solid #ccc;border-radius:6px;width:280px;" />
        <button type="submit" style="padding:8px 14px;border-radius:6px;border:none;background:#0E6E52;color:white;">Rechercher</button>
      </form>
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="text-align:left;border-bottom:2px solid #333;">
            <th style="padding:10px;">Nom</th>
            <th style="padding:10px;">Téléphone</th>
            <th style="padding:10px;">Rôle</th>
            <th style="padding:10px;">Solde</th>
            <th style="padding:10px;">KYC</th>
            <th style="padding:10px;">Statut</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </body></html>
    """
    return HTMLResponse(content=html)


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
      <p><a href="/api/v1/admin/users-page?secret={secret}">← Voir la liste des utilisateurs</a></p>
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
