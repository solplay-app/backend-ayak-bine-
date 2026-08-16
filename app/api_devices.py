"""/devices/register — enregistrement du token push FCM d'un appareil."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import DeviceToken, User
from app.schemas import DeviceRegisterRequest

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])


@router.post("/register")
async def register_device(
    payload: DeviceRegisterRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upsert : un même token FCM est réassigné au dernier utilisateur connecté."""
    existing = (
        await db.execute(select(DeviceToken).where(DeviceToken.token == payload.fcm_token))
    ).scalar_one_or_none()

    if existing:
        existing.user_id = user.id
        existing.platform = payload.platform
    else:
        db.add(DeviceToken(user_id=user.id, token=payload.fcm_token, platform=payload.platform))

    await db.commit()
    return {"success": True}
