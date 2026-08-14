"""/kyc/submit et /kyc/status — vérification d'identité utilisateur."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import KycStatus, KycSubmission, User
from app.schemas import KycStatusResponse, KycSubmitRequest

router = APIRouter(prefix="/api/v1/kyc", tags=["KYC"])
settings = get_settings()


@router.post("/submit")
async def submit_kyc(
    payload: KycSubmitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    max_chars = settings.KYC_MAX_IMAGE_BASE64_CHARS
    if len(payload.id_document_base64) > max_chars or (
        payload.selfie_base64 and len(payload.selfie_base64) > max_chars
    ):
        raise HTTPException(status_code=413, detail="Image trop volumineuse")

    existing = (
        await db.execute(
            select(KycSubmission).where(
                KycSubmission.user_id == user.id,
                KycSubmission.status.in_([KycStatus.PENDING, KycStatus.APPROVED]),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Dossier KYC déjà {existing.status.value.lower()} — impossible d'en soumettre un autre",
        )

    submission = KycSubmission(
        user_id=user.id,
        id_document_base64=payload.id_document_base64,
        selfie_base64=payload.selfie_base64,
        status=KycStatus.PENDING,
    )
    db.add(submission)
    await db.commit()
    return {"success": True, "message": "Dossier envoyé, en attente de validation"}


@router.get("/status", response_model=KycStatusResponse)
async def kyc_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    submission = (
        await db.execute(
            select(KycSubmission)
            .where(KycSubmission.user_id == user.id)
            .order_by(KycSubmission.created_at.desc())
        )
    ).scalars().first()

    if not submission:
        return KycStatusResponse(status="NOT_SUBMITTED", message="Aucun dossier envoyé")

    return KycStatusResponse(
        status=submission.status.value,
        message=submission.review_note,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )
