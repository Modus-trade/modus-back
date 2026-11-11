from sqlalchemy import select
from ..models import Notification, TicketParticipant, User


def notify(db, ticket, actor_id, kind):
    recipients = {ticket.author_id, ticket.assignee_id}
    recipients.update(db.scalars(select(User.id).where(User.role == "ADMIN", User.is_active.is_(True))))
    if kind in ("status", "result") and ticket.is_shared:
        recipients.update(db.scalars(select(TicketParticipant.user_id).where(TicketParticipant.ticket_id == ticket.id)))
    recipients.discard(None)
    recipients.discard(actor_id)
    active = db.scalars(select(User.id).where(User.id.in_(recipients), User.is_active.is_(True))).all()
    for recipient in active:
        db.add(Notification(user_id=recipient, ticket_id=ticket.id, kind=kind))
