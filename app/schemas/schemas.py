import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.models import KycStatus, MobileOperator, TransactionStatus, TransactionType
from app.services.fee_rules import MAX_TRANSFER_AMOUNT, MIN_TRANSFER_AMOUNT

CI_PHONE_PATTERN = r"^(\+225)?0[0-9]{9}$"


def _normalize_ci_phone(v: str) -> str:
    v = v.strip().replace(" ", "")
    if v.startswith("+225"):
        return v
    if v.startswith("00225"):
        return "+225" + v[5:]
    if v.startswith("225") and len(v) == 13:
        return "+" + v
    return "+225" + v


class TransferRequest(BaseModel):
    source_operator: MobileOperator
    source_phone: str | None = Field(default=None, min_length=8, max_length=20)
    destination_operator: MobileOperator
    recipient_name: str = Field(min_length=2, max_length=100)
    recipient_phone: str = Field(min_length=8, max_length=20)
    amount: Decimal = Field(description="Montant net que le destinataire doit recevoir, en XOF")
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("source_phone", "recipient_phone")
    @classmethod
    def normalize_phone(cls, v: str | None) -> str | None:
        return _normalize_ci_phone(v) if v else v

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
    redirect_url: str | None = None
    message: str


class TransferDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    internal_reference: str
    type: TransactionType
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


class DepositRequest(BaseModel):
    operator: MobileOperator
    source_phone: str | None = Field(default=None, min_length=8, max_length=20)
    amount: Decimal = Field(gt=0, description="Montant à déposer sur le wallet, en XOF")
    pin_code: str = Field(min_length=4, max_length=4)

    @field_validator("source_phone")
    @classmethod
    def normalize_source_phone(cls, v: str | None) -> str | None:
        return _normalize_ci_phone(v) if v else v


class DepositResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    amount: Decimal
    message: str
    # --- Nécessaire pour ouvrir le widget Kkiapay côté app Flutter ---
    kkiapay_public_key: str
    kkiapay_sandbox: bool


class DepositConfirmRequest(BaseModel):
    internal_reference: str
    kkiapay_transaction_id: str = Field(min_length=1, max_length=100)


class DepositConfirmResponse(BaseModel):
    internal_reference: str
    status: TransactionStatus
    message: str


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
    pin_required: bool


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    phone_number: str
    kyc_status: KycStatus
    created_at: datetime


class SetPinRequest(BaseModel):
    pin_code: str = Field(min_length=4, max_length=4)


class RegisterDeviceRequest(BaseModel):
    fcm_token: str = Field(min_length=10, max_length=255)


class JekoMoneyModel(BaseModel):
    amount: int
    currency: str


class JekoTransactionDetails(BaseModel):
    id: str | None = None
    reference: str | None = None
    paymentLinkId: str | None = None


class JekoWebhookTransactionData(BaseModel):
    id: str
    amount: JekoMoneyModel
    fees: JekoMoneyModel
    status: str
    counterpartLabel: str | None = None
    counterpartIdentifier: str | None = None
    paymentMethod: str | None = None
    transactionType: str
    businessName: str | None = None
    storeName: str | None = None
    description: str | None = None
    executedAt: str | None = None
    transactionDetails: JekoTransactionDetails | None = None


class JekoWebhookPayload(BaseModel):
    event: str
    data: JekoWebhookTransactionData
    timestamp: str | None = None


# ---------- Vérification d'identité (KYC), validation manuelle ----------

class KycSubmitRequest(BaseModel):
    # Images encodées en base64 par l'app avant l'envoi (voir
    # kyc_verification_screen.dart côté Flutter). Champ obligatoire :
    # photo de la pièce d'identité. Selfie optionnel mais recommandé.
    id_document_base64: str = Field(min_length=100)
    selfie_base64: str | None = Field(default=None, min_length=100)


class KycSubmitResponse(BaseModel):
    id: uuid.UUID
    status: str
    message: str


class KycStatusResponse(BaseModel):
    kyc_status: str  # PENDING / VERIFIED / REJECTED (statut global du compte, sur User)
    submission_status: str | None = None  # UNDER_REVIEW / VERIFIED / REJECTED (dernière demande, s'il y en a une)
    rejection_reason: str | None = None
    submitted_at: datetime | None = None
