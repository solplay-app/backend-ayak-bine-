import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.models import MobileOperator, TransactionStatus, TransactionType

CI_PHONE_PATTERN = r"^(\+225)?0?[0-9]{10}$"


# ---------- Wallet / Deposit (Pay-In) ----------

class DepositRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=2_000_000, description="Montant en XOF")
    operator: MobileOperator
    phone_number: str = Field(min_length=8, max_length=20)
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not v.startswith("+225"):
            v = "+225" + v.lstrip("0")
        return v


class DepositResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    payment_link: str | None = None
    message: str


# ---------- Wallet / Withdraw (Pay-Out) ----------

class WithdrawRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=2_000_000)
    operator: MobileOperator
    phone_number: str = Field(min_length=8, max_length=20)
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not v.startswith("+225"):
            v = "+225" + v.lstrip("0")
        return v


class WithdrawResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    message: str


# ---------- Transaction (lecture) ----------

class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    internal_reference: str
    jeko_reference: str | None
    type: TransactionType
    operator: MobileOperator | None
    amount: Decimal
    fee: Decimal
    status: TransactionStatus
    recipient_phone: str | None
    created_at: datetime


# ---------- Auth (OTP) ----------

class RequestOtpRequest(BaseModel):
    phone_number: str = Field(min_length=8, max_length=20)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not v.startswith("+225"):
            v = "+225" + v.lstrip("0")
        return v


class VerifyOtpRequest(BaseModel):
    phone_number: str = Field(min_length=8, max_length=20)
    otp_code: str = Field(min_length=6, max_length=6)
    full_name: str | None = Field(default=None, max_length=100)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not v.startswith("+225"):
            v = "+225" + v.lstrip("0")
        return v


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    pin_required: bool  # true si l'utilisateur doit encore définir son PIN (premier login)


class SetPinRequest(BaseModel):
    pin_code: str = Field(min_length=4, max_length=4)


class WalletBalanceResponse(BaseModel):
    balance: Decimal
    currency: str


class RegisterDeviceRequest(BaseModel):
    fcm_token: str = Field(min_length=10, max_length=255)


# ---------- Webhook JEKO ----------

class JekoWebhookPayload(BaseModel):
    """Structure générique du webhook JEKO (Pay-In & Pay-Out)."""

    reference: str = Field(description="Référence interne envoyée à JEKO (internal_reference)")
    jeko_transaction_id: str
    status: str  # ex: SUCCESS / FAILED / PENDING côté JEKO
    amount: Decimal
    operator: str | None = None
    failure_reason: str | None = None
    raw: dict[str, Any] | None = None
