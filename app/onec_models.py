from datetime import datetime
from sqlalchemy import String, Text, DateTime, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import now

class OneCSettings(Base):
    __tablename__ = 'onec_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(10), default='mock')
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    health: Mapped[str] = mapped_column(Text, default='Демонстрационный каталог')

class OneCRecord(Base):
    __tablename__ = 'onec_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(30))
    ref: Mapped[str] = mapped_column(String(36))
    payload: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint('scope','kind','ref'),)

class OneCExchange(Base):
    __tablename__ = 'onec_exchanges'
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    operation: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
