import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.models import MobileOperator, TransactionStatus, TransactionType
from app.services.fee_rules import MAX_TRANSFER_AMOUNT, MIN_TRANSFER_AMOUNT

CI_PHONE_PATTERN = r"^(\+225)?0[0-9]{9}$"


def _normalize_ci_phone(v: str) -> str:
    """
    Normalise un numéro ivoirien saisi sous forme locale (ex: '07 79 32 16 19'
    ou '0779321619') vers le format international E.164 (+2250779321619).

    ⚠️ Depuis la réforme de la numérotation ivoirienne (2021), le zéro
    initial fait partie intégrante du numéro à 10 chiffres (ce n'est PAS un
    préfixe interurbain à retirer, contrairement à d'anciens plans de
    numérotation d'autres pays) : +225 0779321619, jamais +225 779321619.
    Le retirer produit un numéro à 9 chiffres invalide, rejeté par JEKO
    (\"payerPhone field format is invalid\").
    """
    v = v.strip().replace(" ", "")
    if v.startswith("+225"):
        return v
    if v.startswith("00225"):
        return "+225" + v[5:]
    if v.startswith("225") and len(v) == 13:
        return "+" + v
    # Format local (0779321619, 10 chiffres) : on conserve le 0 initial.
    return "+225" + v


# ---------- Transfert inter-opérateurs (2 étapes chaînées : pay-in + pay-out) ----------

class TransferRequest(BaseModel):
    source_operator: MobileOperator = Field(description="Opérateur d'où part l'argent (compte connecté)")
    destination_operator: MobileOperator
    recipient_name: str = Field(min_length=2, max_length=100)
    recipient_phone: str = Field(min_length=8, max_length=20)
    amount: Decimal = Field(description="Montant NET que le destinataire doit recevoir, en XOF")
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("recipient_phone")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return _normalize_ci_phone(v)

    @field_validator("amount")
    @classmethod
    def check_amount_range(cls, v: Decimal) -> Decimal:
        if v < MIN_TRANSFER_AMOUNT or v > MAX_TRANSFER_AMOUNT:
            raise ValueError(f"Le montant doit être compris entre {MIN_TRANSFER_AMOUNT} et {MAX_TRANSFER_AMOUNT} XOF")
        return v


class TransferResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    net_amount: Decimal
    platform_fee: Decimal
    total_to_pay: Decimal
    redirect_url: str | None = Field(default=None, description="URL à ouvrir pour confirmer le paiement côté client")
    message: str


class TransferDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    internal_reference: str
    source_operator: MobileOperator | None
    destination_operator: MobileOperator | None
    amount: Decimal
    fee: Decimal
    total_collected: Decimal | None
    payin_status: TransactionStatus
    payout_status: TransactionStatus | None
    status: TransactionStatus
    recipient_name: str | None
    recipient_phone: str | None
    created_at: datetime


# ---------- Retrait wallet -> Mobile Money (1 seule étape : pay-out) ----------

class WithdrawRequest(BaseModel):
    operator: MobileOperator
    amount: Decimal = Field(gt=0, description="Montant à retirer du wallet interne, en XOF")
    pin_code: str = Field(min_length=4, max_length=4)


class WithdrawResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    message: str


class WalletBalanceResponse(BaseModel):
    balance: Decimal
    currency: str


# ---------- Transaction (lecture, historique) ----------

class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    internal_reference: str
    type: TransactionType
    source_operator: MobileOperator | None
    destination_operator: MobileOperator | None
    amount: Decimal
    fee: Decimal
    status: TransactionStatus
    recipient_name: str | None
    recipient_phone: str | None
    created_at: datetime


# ---------- Auth (OTP) ----------

class RequestOtpRequest(BaseModel):
    phone_number: str = Field(min_length=8, max_length=20)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return _normalize_ci_phone(v)


class VerifyOtpRequest(BaseModel):
    phone_number: str = Field(min_length=8, max_length=20)
    otp_code: str = Field(min_length=6, max_length=6)
    full_name: str | None = Field(default=None, max_length=100)

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        return _normalize_ci_phone(v)


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    pin_required: bool  # true si l'utilisateur doit encore définir son PIN (premier login)


class SetPinRequest(BaseModel):
    pin_code: str = Field(min_length=4, max_length=4)


class RegisterDeviceRequest(BaseModel):
    fcm_token: str = Field(min_length=10, max_length=255)


# ---------- Webhook JEKO ----------

class JekoMoneyModel(BaseModel):
    amount: int  # en centimes, cf. doc JEKO
    currency: str


class JekoTransactionDetails(BaseModel):
    id: str | None = None
    reference: str | None = None  # notre {internal_reference}-IN ou -OUT
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
