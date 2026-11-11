"""Уведомления внутри системы и живые события для открытых вкладок и приложений."""
from sqlalchemy import or_, select
from ..models import Notification, Ticket, User
from ..realtime import queue

STATUS_LABELS = {
    "NEW": "Новое",
    "IN_REVIEW": "На рассмотрении",
    "IN_PROGRESS": "В работе",
    "AWAITING_CLARIFICATION": "Ожидает уточнения",
    "IMPLEMENTED": "Реализовано",
    "REJECTED": "Отклонено",
    "CLOSED": "Закрыто",
}


def viewers(db, ticket: Ticket) -> set[int]:
    """Кто может видеть обращение — те же правила, что в services.tickets.visible."""
    author = db.get(User, ticket.author_id)
    ids = set(
        db.scalars(
            select(User.id).where(
                User.is_active,
                or_(
                    User.role == "ADMIN",
                    (User.role == "MANAGER") & (User.department == author.department),
                ),
            )
        )
    )
    ids.add(ticket.author_id)
    if ticket.assignee_id:
        ids.add(ticket.assignee_id)
    return ids


def ticket_changed(db, ticket: Ticket, reason: str):
    """Сообщает всем, кто видит обращение, что его нужно перечитать."""
    queue(db, viewers(db, ticket), {"type": "ticket_changed", "ticket_id": ticket.id, "reason": reason})


def serialize_notification(db, n: Notification) -> dict:
    ticket = db.get(Ticket, n.ticket_id)
    actor = db.get(User, n.actor_id) if n.actor_id else None
    return {
        "id": n.id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "ticket_id": n.ticket_id,
        "ticket_number": ticket.number if ticket else None,
        "ticket_title": ticket.title if ticket else None,
        "actor_name": actor.full_name if actor else None,
        "created_at": n.created_at.isoformat(),
        "read": n.read_at is not None,
    }


def notify(db, user_id: int | None, ticket: Ticket, actor: User, kind: str, title: str, body: str = ""):
    """Создаёт уведомление получателю (кроме самого автора действия) и отправляет его вживую."""
    if not user_id or user_id == actor.id:
        return
    recipient = db.get(User, user_id)
    if not recipient or not recipient.is_active:
        return
    item = Notification(user_id=user_id, ticket_id=ticket.id, actor_id=actor.id, kind=kind, title=title, body=body[:500])
    db.add(item)
    db.flush()
    queue(db, {user_id}, {"type": "notification", "notification": serialize_notification(db, item)})


def short(text: str | None, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
