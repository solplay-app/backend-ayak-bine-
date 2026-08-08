import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TransactionType(str, enum.Enum):
    CASH_IN = "CASH_IN"  # Le client remet du cash à l'agent -> agent crédite le Mobile Money du client (JEKO payout)
    CASH_OUT = "CASH_OUT"  # Le client paie via Mobile Money -> agent lui remet du cash (JEKO payin)
    BILL_PAYMENT = "BILL_PAYMENT"
    INSURANCE = "INSURANCE"
    TRANSFER = "TRANSFER"


class TransactionStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MobileOperator(str, enum.Enum):
    WAVE = "WAVE"
    ORANGE = "ORANGE"
    MTN = "MTN"
    MOOV = "MOOV"


class KycStatus(str, enum.Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class User(Base):
    """
    Représente un AGENT (celui qui se connecte à l'app et opère pour le
    compte de ses clients) — pas un client final. Les clients n'ont pas de
    compte dans ce système : voir le modèle `Client` ci-dessous.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    pin_code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    kyc_status: Mapped[KycStatus] = mapped_column(
        Enum(KycStatus, name="kyc_status"), default=KycStatus.PENDING
    )
    # Taux de commission appliqué par l'agent sur CHAQUE opération (Cash-In
    # comme Cash-Out — l'utilisateur a choisi un taux unique pour les deux).
    # Stocké en fraction décimale : 0.0150 = 1.50%.
    commission_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.01)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="agent")
    clients: Mapped[list["Client"]] = relationship(back_populates="agent")


class Wallet(Base):
    """
    Solde des COMMISSIONS accumulées par l'agent (pas un solde de fonds
    clients : l'argent des opérations transite directement via le "float"
    JEKO de l'agent, visible en temps réel via JekoClient.get_store_balance()
    — jamais mirroré ici pour éviter tout risque de désynchronisation).
    Ce wallet est crédité automatiquement à chaque transaction réussie.
    """

    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    balance: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    currency: Mapped[str] = mapped_column(String(5), default="XOF")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallet_balance_positive"),)

    user: Mapped["User"] = relationship(back_populates="wallet")


class Client(Base):
    """
    Fiche client mémorisée par un agent : évite de re-saisir le nom/numéro à
    chaque opération, et permet de retrouver l'historique d'un client donné.
    Un même numéro de téléphone peut exister chez plusieurs agents différents
    (chacun a sa propre clientèle) -> unicité (agent_id, phone_number), pas
    une unicité globale.
    """

    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agent: Mapped["User"] = relationship(back_populates="clients")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="client")

    __table_args__ = (
        UniqueConstraint("agent_id", "phone_number", name="uq_client_agent_phone"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    internal_reference: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    jeko_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, name="transaction_type"))
    operator: Mapped[MobileOperator | None] = mapped_column(
        Enum(MobileOperator, name="mobile_operator"), nullable=True
    )
    amount: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    # Commission agent sur CETTE opération : montant en XOF, et le taux
    # utilisé pour la calculer (snapshot au moment de la transaction, pour
    # que l'historique reste exact même si l'agent change son taux plus tard).
    commission_amount: Mapped[float] = mapped_column(Numeric(15, 2), default=0)
    commission_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0)
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status"), default=TransactionStatus.PENDING
    )
    recipient_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agent: Mapped["User"] = relationship(back_populates="transactions")
    client: Mapped["Client | None"] = relationship(back_populates="transactions")
