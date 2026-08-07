import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeviceToken(Base):
    """Token FCM d'un appareil Android, utilisé pour notifier l'utilisateur
    quand le webhook JEKO confirme une transaction (même app fermée)."""

    __tablename__ = "device_tokens"
    __table_args__ = (UniqueConstraint("user_id", "fcm_token", name="uq_user_fcm_token"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    fcm_token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), default="ANDROID")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
