import os
import jwt
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, update
from ..models import Notification, User, now
from ..realtime import hub
from ..security import CurrentUser, DB, SECRET
from ..services.notify import serialize_notification

router = APIRouter(prefix="/api", tags=["notifications"])


@router.get("/notifications")
def list_notifications(db: DB, user: CurrentUser, limit: int = Query(30, ge=1, le=100)):
    items = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    ).all()
    unread = db.scalar(
        select(func.count()).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    return {"items": [serialize_notification(db, n) for n in items], "unread": unread}


@router.post("/notifications/{notification_id}/read")
def read_one(notification_id: int, db: DB, user: CurrentUser):
    item = db.get(Notification, notification_id)
    if not item or item.user_id != user.id:
        raise HTTPException(404, "Уведомление не найдено")
    if item.read_at is None:
        item.read_at = now()
        db.commit()
    return {"ok": True}


@router.post("/notifications/read-all")
def read_all(db: DB, user: CurrentUser, ticket_id: int | None = None):
    query = update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    if ticket_id is not None:
        query = query.where(Notification.ticket_id == ticket_id)
    db.execute(query.values(read_at=now()))
    db.commit()
    return {"ok": True}


def _user_from_cookie(db, token: str | None) -> int | None:
    try:
        payload = jwt.decode(token or "", SECRET, algorithms=["HS256"])
        uid = int(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    user = db.get(User, uid)
    return user.id if user and user.is_active else None


@router.websocket("/ws")
async def websocket(ws: WebSocket, db: DB):
    # Защита от чужих сайтов: как и для изменяющих запросов, проверяем Origin.
    origin = ws.headers.get("origin")
    allowed = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin and origin not in allowed:
        await ws.close(code=4403)
        return
    user_id = _user_from_cookie(db, ws.cookies.get("session"))
    db.close()  # соединение с базой на всё время сокета не держим
    if not user_id:
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.connect(user_id, ws)
    try:
        while True:
            # Клиент присылает ping, чтобы соединение не закрывали прокси.
            if await ws.receive_text() == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(user_id, ws)
