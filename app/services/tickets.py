from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from ..models import Ticket, TicketHistory, User, now

TRANSITIONS = {
    "NEW": ["IN_REVIEW", "IN_PROGRESS", "AWAITING_CLARIFICATION", "REJECTED"],
    "IN_REVIEW": ["IN_PROGRESS", "AWAITING_CLARIFICATION", "IMPLEMENTED", "REJECTED"],
    "IN_PROGRESS": ["AWAITING_CLARIFICATION", "IMPLEMENTED", "REJECTED"],
    "AWAITING_CLARIFICATION": ["IN_REVIEW", "IN_PROGRESS", "REJECTED"],
    "IMPLEMENTED": ["CLOSED"],
    "REJECTED": ["CLOSED"],
    "CLOSED": [],
}


def visible(user: User):
    query = select(Ticket)
    if user.role == "EMPLOYEE":
        query = query.where(Ticket.author_id == user.id)
    elif user.role == "MANAGER":
        query = query.where(
            or_(
                Ticket.assignee_id == user.id,
                Ticket.author_id.in_(
                    select(User.id).where(User.department == user.department)
                ),
            )
        )
    return query


def get_ticket(db: Session, user: User, ticket_id: int) -> Ticket:
    ticket = db.scalar(visible(user).where(Ticket.id == ticket_id))
    if not ticket:
        raise HTTPException(404, "Обращение не найдено")
    return ticket


def require_manager(user: User):
    if user.role == "EMPLOYEE":
        raise HTTPException(403, "Недостаточно прав")


def validate_assignee(db: Session, assignee_id: int | None):
    if assignee_id is not None:
        user = db.get(User, assignee_id)
        if not user or not user.is_active or user.role == "EMPLOYEE":
            raise HTTPException(
                422, "Выберите активного руководителя или администратора"
            )


def change(db: Session, ticket: Ticket, actor: User, field: str, value):
    old = getattr(ticket, field)
    if old != value:
        if field in ("status", "priority", "assignee_id"):
            db.add(
                TicketHistory(
                    ticket_id=ticket.id,
                    actor_id=actor.id,
                    field=field,
                    old_value=str(old) if old is not None else None,
                    new_value=str(value) if value is not None else None,
                )
            )
        setattr(ticket, field, value)
        ticket.updated_at = now()


def set_status(db: Session, ticket: Ticket, actor: User, status: str):
    if status == ticket.status:
        return
    if status not in TRANSITIONS[ticket.status]:
        raise HTTPException(422, "Недопустимый переход статуса обращения")
    if (
        status in ("IMPLEMENTED", "REJECTED", "CLOSED")
        and not (ticket.resolution or "").strip()
    ):
        raise HTTPException(
            422, "Укажите результат рассмотрения перед завершением обращения"
        )
    if ticket.status == "NEW" and not ticket.first_response_at:
        ticket.first_response_at = now()
    change(db, ticket, actor, "status", status)
    if status == "CLOSED":
        ticket.closed_at = now()
