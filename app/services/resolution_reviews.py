"""Only the author can accept or return the exact resolution revision they read."""
from fastapi import HTTPException
from sqlalchemy import select, update

from ..models import ResolutionReview, Ticket, TicketHistory, now
from ..schemas import ResolutionReviewCreate


def review_resolution(db, ticket, author, data: ResolutionReviewCreate):
    if author.id != ticket.author_id:
        raise HTTPException(403, "Проверить результат может только автор обращения")
    if data.expected_version != ticket.resolution_review_version:
        raise HTTPException(409, "Результат обновлён. Ознакомьтесь с актуальной версией перед проверкой.")
    previous = db.scalar(select(ResolutionReview).where(
        ResolutionReview.ticket_id == ticket.id,
        ResolutionReview.version == data.expected_version,
    ))
    if previous:
        if previous.decision == data.decision and previous.explanation == data.explanation:
            return False
        raise HTTPException(409, "Эта версия результата уже проверена автором")
    if ticket.status != "IMPLEMENTED":
        raise HTTPException(422, "Проверка доступна после публикации результата «Реализовано»")
    if data.decision == "rework" and not data.explanation:
        raise HTTPException(422, "Поясните, что нужно доработать")
    if data.decision == "confirm" and not (ticket.resolution or "").strip():
        raise HTTPException(422, "Исполнитель ещё не указал результат. Можно вернуть обращение на доработку.")

    timestamp = now()
    confirmed = data.decision == "confirm"
    status = "CLOSED" if confirmed else "IN_PROGRESS"
    values = {
        "status": status,
        "updated_at": timestamp,
        "closed_at": timestamp if confirmed else None,
        "resolution_confirmed_at": timestamp if confirmed else None,
    }
    if not confirmed and ticket.resolution_due_at is not None:
        # Keep the agreed deadline. A new work cycle must be eligible for reminders
        # and escalation even if the previous cycle already sent its notification.
        values["resolution_due_version"] = Ticket.resolution_due_version + 1
    changed = db.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id, Ticket.status == "IMPLEMENTED",
               Ticket.resolution_review_version == data.expected_version)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "Результат уже изменён или проверен. Обновите карточку.")
    db.add(ResolutionReview(
        ticket_id=ticket.id, version=data.expected_version, author_id=author.id,
        decision=data.decision, explanation=data.explanation,
        resolution_snapshot=ticket.resolution, created_at=timestamp,
    ))
    db.add_all([
        TicketHistory(ticket_id=ticket.id, actor_id=author.id, field="status",
                      old_value="IMPLEMENTED", new_value=status,
                      created_at=timestamp),
        TicketHistory(ticket_id=ticket.id, actor_id=author.id, field="resolution_review",
                      new_value=data.decision, reason=data.explanation or None,
                      created_at=timestamp),
    ])
    db.refresh(ticket)
    return True
