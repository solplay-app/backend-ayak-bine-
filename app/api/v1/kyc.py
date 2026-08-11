"""
Vérification d'identité (KYC) — soumission par le client, validation
manuelle par un admin (voir app/api/v1/admin.py pour les routes de revue).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.models import KycSubmission, User
from app.schemas.schemas import KycStatusResponse, KycSubmitRequest, KycSubmitResponse
from app.core.security import get_current_user

router = APIRouter(prefix="/api/v1/kyc", tags=["KYC"])
settings = get_settings()


@router.post("/submit", response_model=KycSubmitResponse)
async def submit_kyc(
    payload: KycSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if len(payload.id_document_base64) > settings.kyc_max_image_base64_chars:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Photo trop volumineuse.")
    if payload.selfie_base64 and len(payload.selfie_base64) > settings.kyc_max_image_base64_chars:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Photo trop volumineuse.")

    # Empêche le spam de demandes : une seule demande "en cours d'examen" à la fois.
    existing = await db.execute(
        select(KycSubmission)
        .where(KycSubmission.user_id == current_user.id, KycSubmission.status == "UNDER_REVIEW")
        .order_by(KycSubmission.submitted_at.desc())
    )
    if existing.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Une demande de vérification est déjà en cours d'examen.",
        )

    submission = KycSubmission(
        user_id=current_user.id,
        id_document_base64=payload.id_document_base64,
        selfie_base64=payload.selfie_base64,
        status="UNDER_REVIEW",
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    return KycSubmitResponse(
        id=submission.id,
        status=submission.status,
        message="Demande envoyée. Vous serez notifié dès qu'elle sera examinée.",
    )


@router.get("/status", response_model=KycStatusResponse)
async def kyc_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KycSubmission)
        .where(KycSubmission.user_id == current_user.id)
        .order_by(KycSubmission.submitted_at.desc())
    )
    latest = result.scalars().first()

    return KycStatusResponse(
        kyc_status=current_user.kyc_status.value,
        submission_status=latest.status if latest else None,
        rejection_reason=latest.rejection_reason if latest else None,
        submitted_at=latest.submitted_at if latest else None,
    )
