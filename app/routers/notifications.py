import os
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select, func, update
from ..models import Notification, Ticket, User, now
from ..realtime import hub
from ..security import CurrentUser, DB, SECRET
from ..services.notifications import serialize
from ..services.tickets import visible

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
live = APIRouter(prefix="/api", tags=["realtime"])


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
    tickets = {t.id: t for t in db.scalars(select(Ticket).where(Ticket.id.in_(ids)))}
    items = [serialize(n, n.ticket_id in allowed, tickets.get(n.ticket_id)) for n in rows]
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


@router.post("/read-ticket/{ticket_id}")
def read_ticket(ticket_id: int, db: DB, user: CurrentUser):
    """Открыли обращение — события по нему считаются прочитанными."""
    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.ticket_id == ticket_id, Notification.read_at.is_(None))
        .values(read_at=now())
    )
    db.commit()
    return {"ok": True}


@live.get("/ws-token")
def ws_token(user: CurrentUser):
    """Короткий токен для WebSocket из мобильного приложения (cookie сессии туда не передать)."""
    token = jwt.encode(
        {"sub": str(user.id), "scope": "ws", "exp": datetime.now(timezone.utc) + timedelta(seconds=60)},
        SECRET,
        algorithm="HS256",
    )
    return {"token": token}


def _user_from_cookie(db, token: str | None, scope: str | None = None) -> int | None:
    try:
        payload = jwt.decode(token or "", SECRET, algorithms=["HS256"])
        uid = int(payload["sub"])
        if scope and payload.get("scope") != scope:
            return None
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    user = db.get(User, uid)
    return user.id if user and user.is_active else None


@live.websocket("/ws")
async def websocket(ws: WebSocket, db: DB):
    """Живые события: новые уведомления и изменения обращений без перезагрузки."""
    token = ws.query_params.get("token")
    if token:
        # Мобильное приложение: вход по короткому токену. Чужой сайт его получить не может,
        # поэтому проверка Origin здесь не нужна.
        user_id = _user_from_cookie(db, token, scope="ws")
    else:
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
            if await ws.receive_text() == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(user_id, ws)
