"""Administrative attention queue. Counts overlap; total counts unique tickets."""

from datetime import timedelta
from sqlalchemy import DateTime, case, func, or_, select
from sqlalchemy.orm import Session
from ..models import Attachment, Comment, Ticket, TicketHistory, User, now
from .deadlines import ACTIVE_STATUSES
from .tickets import eligible_assignee_ids


def attention_queue(
    db: Session,
    reason: str,
    stale_days: int,
    q: str,
    priority: str | None,
    page: int,
    page_size: int,
):
    timestamp = now()
    available = eligible_assignee_ids(db)
    activity_dates = [Ticket.updated_at]
    for model in (Comment, Attachment, TicketHistory):
        latest = (
            select(func.max(model.created_at))
            .where(model.ticket_id == Ticket.id)
            .correlate(Ticket)
            .scalar_subquery()
        )
        activity_dates.append(func.coalesce(latest, Ticket.updated_at))
    latest_date = func.greatest if db.get_bind().dialect.name == "postgresql" else func.max
    last_activity = latest_date(*activity_dates, type_=DateTime())
    conditions = {
        "unassigned": or_(
            Ticket.assignee_id.is_(None), Ticket.assignee_id.not_in(available)
        ),
        "overdue": Ticket.first_response_at.is_(None) & (Ticket.due_at < timestamp),
        "resolution_overdue": Ticket.resolution_due_at.is_not(None) & (Ticket.resolution_due_at <= timestamp),
        "stale": last_activity < timestamp - timedelta(days=stale_days),
    }
    any_reason = or_(*conditions.values())
    active = Ticket.status.in_(ACTIVE_STATUSES)
    counts = (
        db.execute(
            select(
                func.count().label("total"),
                *[
                    func.coalesce(func.sum(case((condition, 1), else_=0)), 0).label(key)
                    for key, condition in conditions.items()
                ],
            ).where(active, any_reason)
        )
        .mappings()
        .one()
    )
    query = select(
        Ticket,
        last_activity.label("last_activity_at"),
        *[condition.label(key) for key, condition in conditions.items()],
    )
    query = query.where(active, any_reason if reason == "all" else conditions[reason])
    if q.strip():
        query = query.where(
            or_(
                Ticket.title.icontains(q.strip(), autoescape=True),
                Ticket.description.icontains(q.strip(), autoescape=True),
                Ticket.number.icontains(q.strip(), autoescape=True),
            )
        )
    if priority:
        query = query.where(Ticket.priority == priority)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    severity = sum(case((condition, 1), else_=0) for condition in conditions.values())
    query = (
        query.order_by(
            severity.desc(),
            case({"HIGH": 3, "MEDIUM": 2, "LOW": 1}, value=Ticket.priority).desc(),
            last_activity.asc(),
            Ticket.id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return dict(counts), total, db.execute(query).all(), timestamp
