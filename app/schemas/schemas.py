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

class JekoMoneyModel(BaseModel):
    amount: int  # en centimes, cf. doc JEKO
    currency: str


class JekoTransactionDetails(BaseModel):
    id: str | None = None
    reference: str | None = None  # notre internal_reference, si on l'a fournie à la création
    paymentLinkId: str | None = None


class JekoWebhookTransactionData(BaseModel):
    """Corps du champ `data` du webhook JEKO `transaction.completed`."""

    id: str
    amount: JekoMoneyModel
    fees: JekoMoneyModel
    status: str  # "success" ou "error" (JEKO n'utilise pas de majuscules)
    counterpartLabel: str | None = None
    counterpartIdentifier: str | None = None
    paymentMethod: str | None = None
    transactionType: str  # "payment" ou "transfer"
    businessName: str | None = None
    storeName: str | None = None
    description: str | None = None
    executedAt: str | None = None
    transactionDetails: JekoTransactionDetails | None = None


class JekoWebhookPayload(BaseModel):
    """
    Structure réelle du webhook JEKO (voir developer.jeko.africa/webhooks).
    Un seul type d'événement existe : "transaction.completed".
    """

    event: str
    data: JekoWebhookTransactionData
    timestamp: str | None = None
