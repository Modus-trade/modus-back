from collections import Counter
from typing import Literal
from fastapi import APIRouter, Query
from ..security import Admin, CurrentUser, DB
from ..services.attention import attention_queue
from ..services.tickets import require_manager, visible
from .tickets import preload, serialize
from ..models import Category, now

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/attention")
def attention(
    db: DB,
    user: Admin,
    reason: Literal["all", "unassigned", "overdue", "stale"] = "all",
    stale_days: int = Query(7, ge=1, le=90),
    q: str = Query("", max_length=250),
    priority: Literal["LOW", "MEDIUM", "HIGH"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    counts, total, rows, timestamp = attention_queue(
        db, reason, stale_days, q, priority, page, page_size
    )
    preload(db)
    items = []
    for row in rows:
        item = serialize(db, row[0])
        item["attention_reasons"] = [
            key for key in ("unassigned", "overdue", "stale") if row._mapping[key]
        ]
        item["last_activity_at"] = row._mapping["last_activity_at"]
        items.append(item)
    return {
        "counts": counts,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "stale_days": stale_days,
        "generated_at": timestamp,
        "notifications": {"enabled": True},
    }


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
