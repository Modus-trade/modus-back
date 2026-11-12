"""Durable Bitrix directory, outbox and task links (isolated by portal/mode)."""
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from datetime import timezone

def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)
from datetime import datetime


class IntegrationSettings(Base):
    __tablename__ = 'integration_settings'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(10), default='off')
    instance_id: Mapped[str] = mapped_column(String(40))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health: Mapped[str] = mapped_column(Text, default='Подключение ещё не проверено')
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DirectoryEntry(Base):
    __tablename__ = 'bitrix_directory'
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[str] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint('scope', 'kind', 'external_id'), UniqueConstraint('scope', 'user_id'))


class BitrixTask(Base):
    __tablename__ = 'bitrix_tasks'
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(200))
    ticket_id: Mapped[int] = mapped_column(ForeignKey('tickets.id'))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    external_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    xml_id: Mapped[str] = mapped_column(String(150), unique=True)
    state: Mapped[str] = mapped_column(String(30), default='waiting')
    remote_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    remote_assignee: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remote_deadline: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_completion: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('scope', 'ticket_id'),)


class IntegrationJob(Base):
    __tablename__ = 'integration_jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    notification_id: Mapped[int | None] = mapped_column(ForeignKey('notifications.id'), nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(30), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    next_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class BitrixBot(Base):
    __tablename__ = 'bitrix_bots'
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(200), unique=True)
    external_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health: Mapped[str] = mapped_column(Text, default='Не подключён')


class BotDialog(Base):
    __tablename__ = 'bitrix_bot_dialogs'
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey('bitrix_bots.id'))
    external_user_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    draft: Mapped[str] = mapped_column(Text, default='{}')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint('bot_id', 'external_user_id'),)


class BotReceipt(Base):
    __tablename__ = 'bitrix_bot_receipts'
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey('bitrix_bots.id'))
    event_id: Mapped[int] = mapped_column(Integer)
    external_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    reply: Mapped[str] = mapped_column(Text, default='')
    state: Mapped[str] = mapped_column(String(30), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(150), nullable=True)
    next_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint('bot_id', 'event_id'),)
