from sqlalchemy import func, select
from ..models import Ticket, TicketParticipant


def affected_count():
    return 1 + (select(func.count()).select_from(TicketParticipant)
        .where(TicketParticipant.ticket_id == Ticket.id, TicketParticipant.user_id != Ticket.author_id)
        .correlate(Ticket).scalar_subquery())
