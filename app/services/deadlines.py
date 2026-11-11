"""Resolution deadlines and reminders persisted in the same transaction as notifications."""
import asyncio
import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..database import SessionLocal
from ..models import Notification, Ticket, TicketHistory, User, now
from ..realtime import queue
from ..schemas import ResolutionDeadlinePatch
from .notifications import serialize
from .tickets import visible

ACTIVE_STATUSES = ("NEW", "IN_REVIEW", "IN_PROGRESS", "AWAITING_CLARIFICATION")
CHECK_INTERVAL_SECONDS = 60
logger = logging.getLogger(__name__)


def set_resolution_deadline(db, ticket, actor, data: ResolutionDeadlinePatch):
    if data.resolution_due_at == ticket.resolution_due_at:
        return False
    if ticket.status not in ACTIVE_STATUSES:
        raise HTTPException(422, "Срок решения можно менять только у открытого обращения")
    if data.expected_version != ticket.resolution_due_version:
        raise HTTPException(409, "Срок уже изменён другим пользователем. Загрузите актуальный срок.")
    if ticket.resolution_due_at is not None and not data.reason:
        raise HTTPException(422, "Укажите причину изменения срока решения")
    if data.resolution_due_at is not None and data.resolution_due_at <= now():
        raise HTTPException(422, "Новый срок решения должен быть в будущем")
    old = ticket.resolution_due_at
    # Compare-and-swap also protects SQLite, where SELECT FOR UPDATE is unavailable.
    result = db.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.resolution_due_version == data.expected_version,
               Ticket.status.in_(ACTIVE_STATUSES))
        .values(resolution_due_at=data.resolution_due_at,
                resolution_due_version=Ticket.resolution_due_version + 1, updated_at=now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "Обращение изменилось. Обновите карточку и повторите действие.")
    db.add(TicketHistory(
        ticket_id=ticket.id, actor_id=actor.id, field="resolution_due_at",
        old_value=old.isoformat() if old else None,
        new_value=data.resolution_due_at.isoformat() if data.resolution_due_at else None,
        reason=data.reason or None,
    ))
    db.refresh(ticket)
    return True


def _notify_once(db, ticket, recipient, kind, timestamp):
    key = f"{kind}:{ticket.id}:{ticket.resolution_due_version}:{recipient.id}"
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    notification_id = db.scalar(
        insert(Notification)
        .values(user_id=recipient.id, ticket_id=ticket.id, kind=kind,
                deduplication_key=key, created_at=timestamp)
        .on_conflict_do_nothing(index_elements=["deduplication_key"])
        .returning(Notification.id)
    )
    if notification_id is None:
        return 0
    item = db.get(Notification, notification_id)
    allowed = db.scalar(visible(recipient).with_only_columns(Ticket.id).where(Ticket.id == ticket.id)) is not None
    queue(db, {recipient.id}, {"type": "notification", "notification": serialize(item, allowed, ticket)})
    return 1


def dispatch_deadline_notifications(db, timestamp=None):
    """Caller commits the transaction. A rollback also discards queued live events."""
    timestamp = timestamp or now()
    tickets = db.scalars(
        select(Ticket)
        .where(Ticket.status.in_(ACTIVE_STATUSES), Ticket.resolution_due_at.is_not(None),
               Ticket.resolution_due_at <= timestamp + timedelta(hours=24))
        .order_by(Ticket.id)
        .with_for_update(skip_locked=True)
    ).all()
    admins = db.scalars(select(User).where(User.role == "ADMIN", User.is_active.is_(True))).all()
    sent = 0
    for ticket in tickets:
        if ticket.resolution_due_at <= timestamp:
            for admin in admins:
                sent += _notify_once(db, ticket, admin, "resolution_overdue", timestamp)
        else:
            assignee = db.get(User, ticket.assignee_id) if ticket.assignee_id else None
            if assignee and assignee.is_active and assignee.role in ("ADMIN", "MANAGER"):
                sent += _notify_once(db, ticket, assignee, "resolution_reminder", timestamp)
    return sent


def _dispatch_batch():
    with SessionLocal() as db:
        dispatch_deadline_notifications(db)
        db.commit()


async def deadline_notification_loop(stop: asyncio.Event):
    while not stop.is_set():
        try:
            await asyncio.to_thread(_dispatch_batch)
        except Exception:
            logger.exception("Не удалось проверить сроки решения; повтор через минуту")
        try:
            await asyncio.wait_for(stop.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except TimeoutError:
            pass
