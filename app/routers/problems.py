from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from ..models import Ticket, TicketParticipant, now
from ..security import CurrentUser, DB
from ..services.deduplication import ACTIVE, affected_count, similarity, tokens

router = APIRouter(prefix="/api/problems", tags=["shared problems"])


class SimilarQuery(BaseModel):
    text: str = Field(min_length=3, max_length=20500)
    category_id: int | None = None


def summary(db, ticket, user):
    return {
        "id": ticket.id, "number": ticket.number, "title": ticket.title,
        "status": ticket.status, "category_id": ticket.category_id,
        "affected_count": db.scalar(select(affected_count()).where(Ticket.id == ticket.id)),
        "joined": ticket.author_id == user.id or db.get(TicketParticipant, (ticket.id, user.id)) is not None,
    }


@router.post("/similar")
def similar(data: SimilarQuery, db: DB, user: CurrentUser):
    words = tokens(data.text)
    if len(words) < 2:
        return {"items": []}
    # Match only shared titles; private descriptions never influence discovery.
    candidates = db.scalars(select(Ticket).where(Ticket.is_shared.is_(True), Ticket.type == "PROBLEM", Ticket.status.in_(ACTIVE))).all()
    ranked = []
    for ticket in candidates:
        score = similarity(data.text, ticket.title)
        if score >= .38:
            ranked.append((score + (.03 if ticket.category_id == data.category_id else 0), ticket))
    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return {"items": [summary(db, ticket, user) for _, ticket in ranked[:5]]}


@router.get("/joined")
def joined(db: DB, user: CurrentUser, page: int = Query(1, ge=1)):
    query = select(Ticket).join(TicketParticipant, TicketParticipant.ticket_id == Ticket.id).where(TicketParticipant.user_id == user.id, Ticket.is_shared.is_(True))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Ticket.updated_at.desc(), Ticket.id.desc()).offset((page - 1) * 20).limit(20)).all()
    return {"items": [summary(db, t, user) for t in items], "total": total}


@router.post("/{ticket_id}/join")
def join(ticket_id: int, db: DB, user: CurrentUser):
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if not ticket or not ticket.is_shared or ticket.type != "PROBLEM":
        raise HTTPException(404, "Общая проблема не найдена")
    existing = ticket.author_id == user.id or db.get(TicketParticipant, (ticket.id, user.id)) is not None
    if not existing:
        if ticket.status not in ACTIVE:
            raise HTTPException(409, "Проблема уже завершена. Создайте новое обращение.")
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        db.execute(insert(TicketParticipant).values(ticket_id=ticket.id, user_id=user.id, created_at=now()).on_conflict_do_nothing(index_elements=["ticket_id", "user_id"]))
        # Joining does not reset inactivity or the deadline of the actual work.
    db.commit()
    return summary(db, ticket, user)
