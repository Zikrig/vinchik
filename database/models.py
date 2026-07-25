from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Gender(str, enum.Enum):
    male = "male"
    female = "female"


class LookingFor(str, enum.Enum):
    male = "male"
    female = "female"
    any = "any"


class LikeAction(str, enum.Enum):
    like = "like"
    dislike = "dislike"
    message = "message"


class OrderStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    premium_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_like_notify_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    likes_notify_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 0 = none; 1 = 1d sent; 2 = 3d sent; 3 = 7d sent (reset on activity)
    reengage_level: Mapped[int] = mapped_column(Integer, default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped[Profile | None] = relationship(back_populates="user", uselist=False)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), unique=True
    )
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    city_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    gender: Mapped[Gender | None] = mapped_column(Enum(Gender), nullable=True)
    looking_for: Mapped[LookingFor | None] = mapped_column(Enum(LookingFor), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="profile")


class Like(Base):
    __tablename__ = "likes"
    __table_args__ = (UniqueConstraint("from_user_id", "to_user_id", name="uq_like_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"))
    to_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    action: Mapped[LikeAction] = mapped_column(Enum(LikeAction))
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_seen: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DailyLikeStat(Base):
    __tablename__ = "daily_like_stats"
    __table_args__ = (UniqueConstraint("user_id", "utc_date", name="uq_daily_like"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    utc_date: Mapped[date] = mapped_column(Date)
    count: Mapped[int] = mapped_column(Integer, default=0)


class PremiumPlan(Base):
    __tablename__ = "premium_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    days: Mapped[int] = mapped_column(Integer)
    price_text: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PremiumOrder(Base):
    __tablename__ = "premium_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("premium_plans.id"))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.pending)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class RequiredChannel(Base):
    __tablename__ = "required_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256), default="")
    invite_link: Mapped[str] = mapped_column(String(512), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("from_user_id", "to_user_id", name="uq_report_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    to_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
