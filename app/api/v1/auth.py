from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_current_user, hash_pin
from app.database import get_db, get_redis
from app.models.models import KycStatus, User, Wallet
from app.schemas.schemas import AuthTokenResponse, RequestOtpRequest, SetPinRequest, UserRead, VerifyOtpRequest
from app.services.otp_service import OtpDeliveryFailed, OtpInvalid, OtpRateLimited, generate_and_send_otp, verify_otp
from app.services.sms import SmsProvider, get_sms_provider

router = APIRouter(prefix="/api/v1/auth", tags=["Authentification"])
_UNSET_PIN_MARKER = "UNSET"


@router.post("/request-otp", status_code=status.HTTP_204_NO_CONTENT)
async def request_otp(payload: RequestOtpRequest, redis: Redis = Depends(get_redis), sms: SmsProvider = Depends(get_sms_provider)):
    try:
        await generate_and_send_otp(redis, payload.phone_number, sms)
    except OtpRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except OtpDeliveryFailed as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/verify-otp", response_model=AuthTokenResponse)
async def verify_otp_endpoint(payload: VerifyOtpRequest, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis)):
    try:
        await verify_otp(redis, payload.phone_number, payload.otp_code)
    except OtpInvalid as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    result = await db.execute(select(User).where(User.phone_number == payload.phone_number))
    user = result.scalar_one_or_none()

    pin_required = False
    if user is None:
        user = User(full_name=payload.full_name or "Utilisateur", phone_number=payload.phone_number, pin_code_hash=_UNSET_PIN_MARKER, kyc_status=KycStatus.PENDING)
        db.add(user)
        await db.flush()
        db.add(Wallet(user_id=user.id, balance=0))
        pin_required = True
    elif user.pin_code_hash == _UNSET_PIN_MARKER:
        pin_required = True

    await db.commit()
    token = create_access_token(str(user.id))
    return AuthTokenResponse(access_token=token, pin_required=pin_required)


@router.get("/me", response_model=UserRead)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/set-pin", status_code=status.HTTP_204_NO_CONTENT)
async def set_pin(payload: SetPinRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    current_user.pin_code_hash = hash_pin(payload.pin_code)
    await db.commit()
