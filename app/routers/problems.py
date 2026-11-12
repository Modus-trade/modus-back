from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from uuid import UUID
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from ..models import KnowledgeAttempt, Ticket, TicketParticipant, now
from ..security import CurrentUser, DB
from ..services.problems import affected_count

ACTIVE = ("NEW", "IN_REVIEW", "IN_PROGRESS", "AWAITING_CLARIFICATION")

router = APIRouter(prefix="/api/problems", tags=["shared problems"])


class JoinContext(BaseModel):
    self_service_attempt_id: UUID | None = None


def summary(db, ticket, user):
    return {
        "id": ticket.id, "number": ticket.number, "title": ticket.title,
        "status": ticket.status, "category_id": ticket.category_id,
        "affected_count": db.scalar(select(affected_count()).where(Ticket.id == ticket.id)),
        "joined": ticket.author_id == user.id or db.get(TicketParticipant, (ticket.id, user.id)) is not None,
    }

@router.get("/joined")
def joined(db: DB, user: CurrentUser, page: int = Query(1, ge=1)):
    query = select(Ticket).join(TicketParticipant, TicketParticipant.ticket_id == Ticket.id).where(TicketParticipant.user_id == user.id, Ticket.is_shared.is_(True))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Ticket.updated_at.desc(), Ticket.id.desc()).offset((page - 1) * 20).limit(20)).all()
    return {"items": [summary(db, t, user) for t in items], "total": total}


@router.post("/{ticket_id}/join")
def join(ticket_id: int, db: DB, user: CurrentUser, data: JoinContext | None = None):
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if not ticket or not ticket.is_shared or ticket.type != "PROBLEM":
        raise HTTPException(404, "Общая проблема не найдена")
    attempt = None
    if data and data.self_service_attempt_id is not None:
        attempt = db.scalar(select(KnowledgeAttempt).where(
            KnowledgeAttempt.id == str(data.self_service_attempt_id), KnowledgeAttempt.user_id == user.id,
        ).with_for_update())
        if attempt is None:
            raise HTTPException(404, "Попытка поиска решения не найдена")
        if attempt.ticket_id is not None or (attempt.joined_problem_id is not None and attempt.joined_problem_id != ticket.id):
            raise HTTPException(409, "Эта попытка уже завершена. Откройте новую форму.")
    existing = ticket.author_id == user.id or db.get(TicketParticipant, (ticket.id, user.id)) is not None
    if not existing:
        if ticket.status not in ACTIVE:
            raise HTTPException(409, "Проблема уже завершена. Создайте новое обращение.")
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        db.execute(insert(TicketParticipant).values(ticket_id=ticket.id, user_id=user.id, created_at=now()).on_conflict_do_nothing(index_elements=["ticket_id", "user_id"]))
        # Joining does not reset inactivity or the deadline of the actual work.
    if attempt is not None:
        attempt.joined_problem_id = ticket.id
        attempt.resolved_at = None
        attempt.resolved_article_id = None
        attempt.resolved_version = None
    db.commit()
    return summary(db, ticket, user)
