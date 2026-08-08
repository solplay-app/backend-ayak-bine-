import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.models import MobileOperator, TransactionStatus, TransactionType

CI_PHONE_PATTERN = r"^(\+225)?0?[0-9]{10}$"


def _normalize_ci_phone(v: str) -> str:
    v = v.strip().replace(" ", "")
    if not v.startswith("+225"):
        v = "+225" + v.lstrip("0")
    return v


# ---------- Fiches clients ----------

class ClientCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    phone_number: str = Field(min_length=8, max_length=20)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return _normalize_ci_phone(v)


class ClientRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    phone_number: str
    created_at: datetime


# ---------- Cash-In : le client remet du cash à l'agent, qui crédite le
# Mobile Money du client (JEKO effectue un transfert/payout) ----------

class CashInRequest(BaseModel):
    client_full_name: str = Field(min_length=2, max_length=100)
    client_phone_number: str = Field(min_length=8, max_length=20)
    amount: Decimal = Field(gt=0, le=2_000_000, description="Montant en XOF remis par le client")
    operator: MobileOperator
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("client_phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return _normalize_ci_phone(v)


class CashInResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    commission_amount: Decimal
    message: str


# ---------- Cash-Out : le client paie via Mobile Money, l'agent lui remet
# du cash (JEKO collecte un paiement/payin depuis le client) ----------

class CashOutRequest(BaseModel):
    client_full_name: str = Field(min_length=2, max_length=100)
    client_phone_number: str = Field(min_length=8, max_length=20)
    amount: Decimal = Field(gt=0, le=2_000_000, description="Montant en XOF à remettre en cash au client")
    operator: MobileOperator
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("client_phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return _normalize_ci_phone(v)


class CashOutResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    commission_amount: Decimal
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
    commission_amount: Decimal
    status: TransactionStatus
    recipient_phone: str | None
    client_id: uuid.UUID | None
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


class AgentDashboardResponse(BaseModel):
    """
    Vue d'ensemble affichée à l'agent : le float réel disponible chez JEKO
    (source de vérité, jamais mirroré en base pour éviter toute
    désynchronisation) et les commissions cumulées trackées en interne.
    """

    store_balance: Decimal  # solde réel du magasin JEKO, en XOF
    currency: str
    commission_earned_total: Decimal  # cumul historique, jamais réinitialisé
    commission_rate: Decimal  # taux actuel de l'agent (ex: 0.0150 = 1.50%)


class AgentSettingsUpdate(BaseModel):
    commission_rate: Decimal = Field(ge=0, le=Decimal("0.20"), description="Ex: 0.015 pour 1.5%")


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
