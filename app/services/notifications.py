from sqlalchemy import select
from ..models import Notification, Ticket, TicketParticipant, User
from ..realtime import queue
from .tickets import visible

LABELS = {"assigned": "Назначен ответственный", "comment": "Новый комментарий", "clarification": "Запрошено уточнение", "status": "Изменён статус", "result": "Опубликован результат", "created": "Новое обращение"}


def serialize(n: Notification, allowed: bool, ticket: Ticket | None = None) -> dict:
    """Элемент уведомления: одинаковый в списке и в живом событии. Текст комментария не раскрываем."""
    return {
        "id": n.id,
        "ticket_id": n.ticket_id,
        "ticket_number": ticket.number if ticket else None,
        "kind": n.kind,
        "message": LABELS.get(n.kind, "Обновление обращения"),
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "href": f"/tickets/{n.ticket_id}" if allowed else "/tickets#joined-problems",
    }


def notify(db, ticket, actor_id, kind):
    recipients = {ticket.author_id, ticket.assignee_id}
    recipients.update(db.scalars(select(User.id).where(User.role == "ADMIN", User.is_active.is_(True))))
    if kind in ("status", "result") and ticket.is_shared:
        recipients.update(db.scalars(select(TicketParticipant.user_id).where(TicketParticipant.ticket_id == ticket.id)))
    recipients.discard(None)
    recipients.discard(actor_id)
    active = db.scalars(select(User).where(User.id.in_(recipients), User.is_active.is_(True))).all()
    for recipient in active:
        item = Notification(user_id=recipient.id, ticket_id=ticket.id, kind=kind)
        db.add(item)
        db.flush()
        # Мгновенно отправляем получателю (уйдёт после commit).
        allowed = db.scalar(visible(recipient).with_only_columns(Ticket.id).where(Ticket.id == ticket.id)) is not None
        queue(db, {recipient.id}, {"type": "notification", "notification": serialize(item, allowed, ticket)})
