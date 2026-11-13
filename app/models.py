from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    department: Mapped[str] = mapped_column(String(150))
    position: Mapped[str] = mapped_column(String(150), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    default_assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    sla_first_response_hours: Mapped[int] = mapped_column(Integer, default=48)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Ticket(Base):
    onec_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    onec_item: Mapped[str | None] = mapped_column(Text, nullable=True)
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    source: Mapped[str] = mapped_column(String(20), default="WEB", server_default="WEB")
    type: Mapped[str] = mapped_column(String(20))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[str] = mapped_column(Text)
    proposed_solution: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    priority: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    status: Mapped[str] = mapped_column(String(40), default="NEW")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    resolution_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_due_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resolution_review_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resolution_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = {"sqlite_autoincrement": True}


class ResolutionReview(Base):
    __tablename__ = "resolution_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(20))
    explanation: Mapped[str] = mapped_column(Text, default="")
    resolution_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("ticket_id", "version", name="uq_resolution_review_version"),)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    is_clarification_request: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Голосовое сообщение: запись лежит во вложениях, расшифровка приходит от ИИ позже.
    audio_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"), nullable=True, default=None)
    audio_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)


class TicketHistory(Base):
    __tablename__ = "ticket_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    field: Mapped[str] = mapped_column(String(60))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TicketParticipant(Base):
    __tablename__ = "ticket_participants"
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    deduplication_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (Index("uq_notification_deduplication_key", "deduplication_key", unique=True),)

# Register integration tables in the shared metadata (also used by test databases).
from .integration_models import IntegrationSettings, DirectoryEntry, BitrixTask, IntegrationJob  # noqa: E402,F401

class KnowledgeArticle(Base):
    __tablename__ = "knowledge_articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), unique=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(250))
    summary: Mapped[str] = mapped_column(String(600))
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20))
    department: Mapped[str | None] = mapped_column(String(150), nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    editor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    review_due_at: Mapped[datetime] = mapped_column(DateTime)


class KnowledgeAttempt(Base):
    __tablename__ = "knowledge_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    department: Mapped[str] = mapped_column(String(150))
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True, unique=True)
    joined_problem_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    resolved_article_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_articles.id"), nullable=True)
    resolved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class KnowledgeExposure(Base):
    __tablename__ = "knowledge_exposures"
    attempt_id: Mapped[str] = mapped_column(ForeignKey("knowledge_attempts.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("knowledge_articles.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    helped: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeDraft(Base):
    __tablename__ = "knowledge_drafts"
    article_id: Mapped[int] = mapped_column(ForeignKey("knowledge_articles.id"), primary_key=True)
    title: Mapped[str] = mapped_column(String(250), default="")
    summary: Mapped[str] = mapped_column(String(600), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(20), default="DEPARTMENT")
    owner_department: Mapped[str] = mapped_column(String(150))
    confidential_case: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    version: Mapped[int] = mapped_column(Integer, default=1)
    review_days: Mapped[int] = mapped_column(Integer, default=90)
    editor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("knowledge_articles.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(250))
    summary: Mapped[str] = mapped_column(String(600))
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20))
    department: Mapped[str | None] = mapped_column(String(150), nullable=True)
    editor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    review_due_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint("article_id", "version", name="uq_knowledge_revision"),)


class KnowledgeAudit(Base):
    __tablename__ = "knowledge_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("knowledge_articles.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeReport(Base):
    __tablename__ = "knowledge_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("knowledge_articles.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(2000))
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("article_id", "version", "reporter_id", name="uq_knowledge_report"),)

from .onec_models import OneCSettings, OneCRecord, OneCExchange  # noqa: E402,F401
