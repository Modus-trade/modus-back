from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func, update
from ..models import Notification, Ticket, now
from ..security import CurrentUser, DB
from ..services.tickets import visible

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
LABELS = {"assigned": "Назначен ответственный", "comment": "Новый комментарий", "clarification": "Запрошено уточнение", "status": "Изменён статус", "result": "Опубликован результат", "created": "Новое обращение"}


@router.get("")
def notifications(db: DB, user: CurrentUser, page: int = Query(1, ge=1), unread_only: bool = False):
    own = Notification.user_id == user.id
    unread = db.scalar(select(func.count()).select_from(Notification).where(own, Notification.read_at.is_(None)))
    query = select(Notification).where(own)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Notification.id.desc()).offset((page - 1) * 20).limit(20)).all()
    ids = [n.ticket_id for n in rows]
    allowed = set(db.scalars(visible(user).with_only_columns(Ticket.id).where(Ticket.id.in_(ids))))
    items = [{"id": n.id, "ticket_id": n.ticket_id, "kind": n.kind,
              "message": LABELS.get(n.kind, "Обновление обращения"), "created_at": n.created_at,
              "read_at": n.read_at, "href": f"/tickets/{n.ticket_id}" if n.ticket_id in allowed else "/tickets#joined-problems"} for n in rows]
    return {"items": items, "total": total, "unread_count": unread}


@router.post("/read-all")
def read_all(db: DB, user: CurrentUser):
    db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None)).values(read_at=now()))
    db.commit()
    return {"ok": True}


@router.post("/{notification_id}/read")
def read(notification_id: int, db: DB, user: CurrentUser):
    item = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id))
    if not item:
        raise HTTPException(404, "Уведомление не найдено")
    if item.read_at is None:
        item.read_at = now()
        db.commit()
    return {"ok": True}
