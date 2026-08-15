"""Schémas Pydantic v2 — entrées / sorties HTTP."""
from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    phone_number: str = Field(..., min_length=6, max_length=20)
    full_name: str | None = Field(default=None, max_length=120)
    pin_code: str = Field(..., min_length=4, max_length=8)

    @field_validator("phone_number")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().replace(" ", "")


class LoginRequest(BaseModel):
    phone_number: str
    pin_code: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class BootstrapAdminRequest(BaseModel):
    admin_phone: str
    admin_pin: str
    bootstrap_secret: str


class PayInDeclareRequest(BaseModel):
    """L'utilisateur déclare avoir transféré X FCFA sur le compte marchand."""
    amount: Decimal = Field(..., gt=0)
    provider: str           # WAVE ou ORANGE_MONEY
    phone_number: str
    # preuve fournie par le client (SMS opérateur ou ID dépôt Wave/OM)
    proof_ref: str | None = None
    # Méthode : le client note lui-même après son transfert,
    # OU le webhook/SMS vient la remplir automatiquement.
    declared_via: str = "CLIENT_DECLARATION"  # ou "WAVE_WEBHOOK", "ORANGE_SMS", "ADMIN"


class PayInWebhookPayload(BaseModel):
    """Schéma générique d'un webhook Pay-In opérateur."""
    reference: str | None = None          # notre reference interne (PI-XXX) si connu
    provider: str                         # WAVE ou ORANGE_MONEY
    proof_ref: str                        # ID transaction opérateur (unique côté eux)
    amount: Decimal = Field(..., gt=0)
    phone_number: str
    status: str = "SUCCESS"               # SUCCESS / FAILED
    signature: str | None = None          # HMAC-SHA256 calculé sur le corps brut


class SMSListenerPayload(BaseModel):
    """Notification SMS opérateur parsée (Twilio webhook-out)."""
    provider: str                         # WAVE ou ORANGE_MONEY
    body: str                             # corps brut du SMS
    from_number: str | None = None
    token: str                            # = SMS_LISTENER_TOKEN


class PayOutRequest(BaseModel):
    amount: Decimal = Field(..., gt=0)
    provider: str                         # WAVE ou ORANGE_MONEY
    phone_number: str


class AdminActionRequest(BaseModel):
    action: str                           # APPROVE ou REJECT
    proof_ref: str | None = None
    note: str | None = None


class TransactionOut(BaseModel):
    id: uuid.UUID
    reference: str
    type: str
    provider: str
    amount: Decimal
    fee: Decimal
    status: str
    phone_number: str | None
    proof_ref: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WalletOut(BaseModel):
    user_phone: str
    balance: Decimal
    currency: str
    updated_at: datetime

    class Config:
        from_attributes = True


class PayInResult(BaseModel):
    success: bool
    message: str
    reference: str | None = None
    new_balance: Decimal | None = None
    transaction_id: uuid.UUID | None = None


class UserMeResponse(BaseModel):
    id: uuid.UUID
    phone_number: str
    full_name: str
    role: str
    created_at: datetime

    class Config:
        from_attributes = True


class KycSubmitRequest(BaseModel):
    id_document_base64: str = Field(..., min_length=10)
    selfie_base64: str | None = None


class KycStatusResponse(BaseModel):
    status: str
    message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeviceRegisterRequest(BaseModel):
    fcm_token: str = Field(..., min_length=10)
    platform: str = "android"


class InternalTransferRequest(BaseModel):
    amount: Decimal = Field(..., gt=0)
    recipient_phone: str = Field(..., min_length=6)


class InternalTransferResult(BaseModel):
    success: bool
    message: str
    sender_reference: str | None = None
    recipient_reference: str | None = None
    net_amount: Decimal | None = None
    fee: Decimal | None = None
    total_charged: Decimal | None = None
    new_balance: Decimal | None = None


class FeePercentResponse(BaseModel):
    fee_percent: Decimal


class FeePercentUpdateRequest(BaseModel):
    fee_percent: Decimal = Field(..., ge=0, le=100)


class DashboardStatsResponse(BaseModel):
    solde_global: Decimal
    recu_aujourdhui: Decimal
    retire_aujourdhui: Decimal
    frais_collectes_aujourdhui: Decimal
    frais_collectes_total: Decimal
    fee_percent: Decimal
    nb_utilisateurs: int
    nb_payouts_en_attente: int


class AdminUserOut(BaseModel):
    id: str
    phone_number: str
    full_name: str
    role: str
    is_active: bool
    kyc_status: str | None = None
    wallet_balance: Decimal
    created_at: datetime


class UserStatusUpdateRequest(BaseModel):
    is_active: bool


class KycDecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    reason: str | None = None


class DailyStatPoint(BaseModel):
    date: str
    recu: Decimal
    retire: Decimal
    frais: Decimal
