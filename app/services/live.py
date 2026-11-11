"""Живые события: кому из открытых вкладок и приложений сообщать об изменении обращения."""
from sqlalchemy import or_, select
from ..models import Ticket, TicketParticipant, User
from ..realtime import queue


def viewers(db, ticket: Ticket) -> set[int]:
    """Кто может видеть обращение: автор, ответственный, руководители подразделения автора,
    администраторы и присоединившиеся к общей проблеме."""
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
    if ticket.is_shared:
        ids.update(db.scalars(select(TicketParticipant.user_id).where(TicketParticipant.ticket_id == ticket.id)))
    return ids


def ticket_changed(db, ticket: Ticket, reason: str):
    """Просим всех, кто видит обращение, перечитать данные."""
    queue(db, viewers(db, ticket), {"type": "ticket_changed", "ticket_id": ticket.id, "reason": reason})
