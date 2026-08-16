"""/register et /login USER + /admin/bootstrap."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, get_current_user, hash_pin, verify_pin
from app.config import get_settings
from app.database import get_db
from app.models import User, UserRole, Wallet
from app.schemas import (
    BootstrapAdminRequest, LoginRequest, RegisterRequest, TokenResponse,
    UserMeResponse, UserMeUpdateRequest,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentification"])
settings = get_settings()


@router.get("/me", response_model=UserMeResponse)
async def get_me(user: User = Depends(get_current_user)):
    """Retourne les informations du compte actuellement connecté."""
    return UserMeResponse(
        id=user.id,
        phone_number=user.phone_number,
        full_name=user.full_name,
        role=user.role.value,
        created_at=user.created_at,
    )


@router.put("/me", response_model=UserMeResponse)
async def update_me(
    payload: UserMeUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Permet à l'utilisateur connecté de modifier ses informations (nom)."""
    if payload.full_name is not None:
        cleaned = payload.full_name.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="Le nom ne peut pas être vide")
        user.full_name = cleaned
    await db.commit()
    await db.refresh(user)
    return UserMeResponse(
        id=user.id,
        phone_number=user.phone_number,
        full_name=user.full_name,
        role=user.role.value,
        created_at=user.created_at,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(User).where(User.phone_number == payload.phone_number))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Numéro déjà enregistré")

    user = User(
        phone_number=payload.phone_number,
        full_name=payload.full_name or "Utilisateur",
        pin_code_hash=hash_pin(payload.pin_code),
        role=UserRole.USER,
    )
    db.add(user)
    await db.flush()
    db.add(Wallet(user_id=user.id))
    await db.commit()
    await db.refresh(user)

    token = create_access_token(str(user.id), user.role.value)
    return TokenResponse(access_token=token, role=user.role.value)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.phone_number == payload.phone_number))).scalar_one_or_none()
    if not user or not verify_pin(payload.pin_code, user.pin_code_hash):
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Compte désactivé")
    token = create_access_token(str(user.id), user.role.value)
    return TokenResponse(access_token=token, role=user.role.value)


@router.post("/admin/bootstrap", response_model=TokenResponse)
async def bootstrap_admin(payload: BootstrapAdminRequest, db: AsyncSession = Depends(get_db)):
    """
    Crée le premier administrateur — utilisable UNE seule fois.
    Sécurisé par ADMIN_BOOTSTRAP_SECRET, à changer ensuite
    (= vidage complet par script ou ALTER USER).
    """
    if payload.bootstrap_secret != settings.ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=403, detail="Secret de bootstrap invalide")

    existing_admin = (
        await db.execute(select(User).where(User.role == UserRole.ADMIN))
    ).scalar_one_or_none()
    if existing_admin:
        raise HTTPException(status_code=409, detail="Un admin existe déjà — bootstrap fermé")

    admin = User(
        phone_number=payload.admin_phone,
        full_name="Administrateur",
        pin_code_hash=hash_pin(payload.admin_pin),
        role=UserRole.ADMIN,
    )
    db.add(admin)
    await db.flush()
    db.add(Wallet(user_id=admin.id))
    await db.commit()

    token = create_access_token(str(admin.id), admin.role.value)
    return TokenResponse(access_token=token, role=admin.role.value)
