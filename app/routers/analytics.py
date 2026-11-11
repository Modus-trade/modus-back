from collections import Counter
from fastapi import APIRouter
from ..security import CurrentUser, DB
from ..services.tickets import require_manager, visible
from .tickets import preload, serialize
from ..models import Category, now

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/summary")
def summary(db: DB, user: CurrentUser):
    require_manager(user)
    preload(db)
    tickets = db.scalars(visible(user)).all()
    total = len(tickets)
    responses = [
        (t.first_response_at - t.created_at).total_seconds() / 3600
        for t in tickets
        if t.first_response_at
    ]
    overdue = [t for t in tickets if t.first_response_at is None and t.due_at < now()]
    return {
        "total": total,
        "by_type": dict(Counter(t.type for t in tickets)),
        "by_status": dict(Counter(t.status for t in tickets)),
        "by_category": dict(
            Counter(db.get(Category, t.category_id).name for t in tickets)
        ),
        "closed_percent": round(
            sum(t.status == "CLOSED" for t in tickets) / total * 100, 1
        )
        if total
        else 0,
        "average_first_response_hours": round(sum(responses) / len(responses), 1)
        if responses
        else None,
        "overdue_count": len(overdue),
        "overdue_tickets": [serialize(db, t) for t in overdue],
    }
