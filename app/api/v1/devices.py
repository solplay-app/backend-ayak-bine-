from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.device_token import DeviceToken
from app.models.models import User
from app.schemas.schemas import RegisterDeviceRequest

router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])


@router.post("/register", status_code=status.HTTP_204_NO_CONTENT)
async def register_device(
    payload: RegisterDeviceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Enregistre (ou met à jour) le token FCM de l'appareil Android courant,
    utilisé pour notifier l'utilisateur des confirmations de transaction
    même quand l'application est fermée. À appeler à chaque démarrage de
    l'app et à chaque rafraîchissement du token (FirebaseMessaging.onTokenRefresh).
    """
    result = await db.execute(
        select(DeviceToken).where(
            DeviceToken.user_id == current_user.id,
            DeviceToken.fcm_token == payload.fcm_token,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        db.add(DeviceToken(user_id=current_user.id, fcm_token=payload.fcm_token))
        await db.commit()
