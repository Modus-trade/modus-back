from datetime import timedelta
from pathlib import Path
import uuid
from typing import Annotated
from pydantic import BeforeValidator
from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select, text
from ..database import BASE_DIR
from ..models import Attachment, Category, Comment, Ticket, TicketHistory, User, now
from ..schemas import CommentCreate, TicketCreate, TicketPatch
from ..security import CurrentUser, DB
from ..services.notify import STATUS_LABELS, notify, short, ticket_changed
from ..services.tickets import (
    change,
    get_ticket,
    require_manager,
    set_status,
    validate_assignee,
    visible,
)

router = APIRouter(prefix="/api", tags=["tickets"])
UPLOADS = BASE_DIR / "uploads"


def empty_to_none(value):
    return None if value == "" else value


OptionalIntFilter = Annotated[int | None, BeforeValidator(empty_to_none)]
OptionalBoolFilter = Annotated[bool | None, BeforeValidator(empty_to_none)]


def preload(db):
    """Загружает пользователей и категории одним запросом каждые.

    Дальше db.get(User/Category, id) берёт их из памяти сессии, без похода в базу —
    это важно, когда база удалённая.
    """
    # Сессия хранит объекты по слабым ссылкам — держим их до конца запроса.
    db.info["preloaded"] = (db.scalars(select(User)).all(), db.scalars(select(Category)).all())


def serialize(db, ticket):
    data = jsonable_encoder(ticket)
    author, assignee, category = (
        db.get(User, ticket.author_id),
        db.get(User, ticket.assignee_id) if ticket.assignee_id else None,
        db.get(Category, ticket.category_id),
    )
    data.update(
        author_name=author.full_name,
        department=author.department,
        assignee_name=assignee.full_name if assignee else None,
        category_name=category.name,
        overdue=ticket.first_response_at is None and now() > ticket.due_at,
    )
    return data


@router.get("/tickets")
def list_tickets(
    db: DB,
    user: CurrentUser,
    type: str | None = None,
    category_id: OptionalIntFilter = None,
    status: str | None = None,
    priority: str | None = None,
    department: str | None = None,
    assignee_id: OptionalIntFilter = None,
    author_id: OptionalIntFilter = None,
    overdue: OptionalBoolFilter = None,
    unassigned: bool = False,
    q: str = "",
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    preload(db)
    query = visible(user)
    for field, value in [
        ("type", type),
        ("category_id", category_id),
        ("status", status),
        ("priority", priority),
        ("assignee_id", assignee_id),
        ("author_id", author_id),
    ]:
        if value is not None and value != "":
            query = query.where(getattr(Ticket, field) == value)
    if department:
        query = query.where(
            Ticket.author_id.in_(select(User.id).where(User.department == department))
        )
    if unassigned:
        if user.role != "ADMIN":
            raise HTTPException(403, "Раздел доступен администратору")
        active_assignees = select(User.id).where(
            User.is_active, User.role.in_(["MANAGER", "ADMIN"])
        )
        query = query.where(
            Ticket.status != "CLOSED",
            or_(Ticket.assignee_id.is_(None), Ticket.assignee_id.not_in(active_assignees)),
        )
    if overdue is not None:
        condition = Ticket.first_response_at.is_(None) & (Ticket.due_at < now())
        query = query.where(condition if overdue else ~condition)
    if q.strip():
        query = query.where(
            or_(
                Ticket.title.icontains(q.strip(), autoescape=True),
                Ticket.description.icontains(q.strip(), autoescape=True),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    column = (
        getattr(Ticket, sort)
        if sort
        in ("created_at", "updated_at", "due_at", "number", "priority", "status")
        else Ticket.created_at
    )
    if sort == "priority":
        column = case({"LOW": 1, "MEDIUM": 2, "HIGH": 3}, value=Ticket.priority, else_=0)
    query = (
        query.order_by(
            column.asc() if order == "asc" else column.desc(), Ticket.id.desc()
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize(db, t) for t in db.scalars(query)],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def next_ticket_id(db) -> int:
    """Выдаёт следующий id обращения, безопасно при одновременных запросах."""
    if db.get_bind().dialect.name == "postgresql":
        return db.execute(
            text("SELECT nextval(pg_get_serial_sequence('tickets', 'id'))")
        ).scalar_one()
    # SQLite: блокировка записи на время выдачи номера.
    db.rollback()
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    last = db.execute(
        text("SELECT seq FROM sqlite_sequence WHERE name='tickets'")
    ).scalar()
    return (last or 0) + 1


@router.post("/tickets", status_code=201)
def create_ticket(data: TicketCreate, db: DB, user: CurrentUser):
    category = db.get(Category, data.category_id)
    if not category or not category.is_active:
        raise HTTPException(422, "Выберите активную категорию")
    assignee = (
        db.get(User, category.default_assignee_id)
        if category.default_assignee_id
        else None
    )
    assignee_id = (
        assignee.id
        if assignee and assignee.is_active and assignee.role in ("MANAGER", "ADMIN")
        else None
    )
    # Номер обращения совпадает с его id: A-001, A-002…
    ticket_id = next_ticket_id(db)
    ticket = Ticket(
        **data.model_dump(),
        id=ticket_id,
        number=f"A-{ticket_id:03d}",
        author_id=user.id,
        assignee_id=assignee_id,
        due_at=now() + timedelta(hours=category.sla_first_response_hours),
    )
    db.add(ticket)
    db.flush()
    notify(db, assignee_id, ticket, user, "assigned", f"Новое обращение {ticket.number}", ticket.title)
    ticket_changed(db, ticket, "created")
    db.commit()
    return serialize(db, ticket)


@router.get("/tickets/{ticket_id}")
def detail(ticket_id: int, db: DB, user: CurrentUser):
    preload(db)
    ticket = get_ticket(db, user, ticket_id)
    result = serialize(db, ticket)
    result["comments"] = [
        {**jsonable_encoder(c), "author_name": db.get(User, c.author_id).full_name}
        for c in db.scalars(
            select(Comment)
            .where(Comment.ticket_id == ticket_id)
            .order_by(Comment.created_at, Comment.id)
        )
    ]
    result["history"] = [
        {**jsonable_encoder(h), "actor_name": db.get(User, h.actor_id).full_name}
        for h in db.scalars(
            select(TicketHistory)
            .where(TicketHistory.ticket_id == ticket_id)
            .order_by(TicketHistory.created_at, TicketHistory.id)
        )
    ]
    result["attachments"] = [
        {k: v for k, v in jsonable_encoder(a).items() if k != "stored_path"}
        for a in db.scalars(select(Attachment).where(Attachment.ticket_id == ticket_id))
    ]
    return result


@router.patch("/tickets/{ticket_id}")
def patch_ticket(ticket_id: int, data: TicketPatch, db: DB, user: CurrentUser):
    require_manager(user)
    ticket = get_ticket(db, user, ticket_id)
    values = data.model_dump(exclude_unset=True)
    if ticket.status == "CLOSED" and values:
        raise HTTPException(422, "Закрытое обращение нельзя изменять")
    if any(v is None for k, v in values.items() if k in ("status", "priority")):
        raise HTTPException(422, "Статус и приоритет не могут быть пустыми")
    if "assignee_id" in values:
        validate_assignee(db, values["assignee_id"])
    before = (ticket.status, ticket.assignee_id, ticket.resolution)
    if "resolution" in values:
        change(db, ticket, user, "resolution", values.pop("resolution"))
    if "status" in values:
        set_status(db, ticket, user, values.pop("status"))
    if (
        ticket.status in ("IMPLEMENTED", "REJECTED")
        and not (ticket.resolution or "").strip()
    ):
        raise HTTPException(422, "Результат рассмотрения обязателен")
    for field, value in values.items():
        change(db, ticket, user, field, value)
    old_status, old_assignee, old_resolution = before
    if ticket.assignee_id != old_assignee:
        notify(db, ticket.assignee_id, ticket, user, "assigned", f"Вас назначили ответственным · {ticket.number}", ticket.title)
    final = ticket.status in ("IMPLEMENTED", "REJECTED")
    if final and (ticket.status != old_status or ticket.resolution != old_resolution):
        label = "Отклонено" if ticket.status == "REJECTED" else "Решение готово"
        notify(db, ticket.author_id, ticket, user, "result", f"{label} · {ticket.number}", short(ticket.resolution))
    elif ticket.status != old_status:
        notify(db, ticket.author_id, ticket, user, "status", f"Статус: {STATUS_LABELS[ticket.status]} · {ticket.number}", ticket.title)
    ticket_changed(db, ticket, "updated")
    db.commit()
    return serialize(db, ticket)


@router.post("/tickets/{ticket_id}/comments", status_code=201)
def comment(ticket_id: int, data: CommentCreate, db: DB, user: CurrentUser):
    ticket = get_ticket(db, user, ticket_id)
    if not data.body.strip():
        raise HTTPException(422, "Введите текст комментария")
    if ticket.status in ("CLOSED", "IMPLEMENTED", "REJECTED"):
        raise HTTPException(422, "Обсуждение завершённого обращения закрыто")
    if data.is_clarification_request:
        require_manager(user)
        set_status(db, ticket, user, "AWAITING_CLARIFICATION")
    elif ticket.status == "AWAITING_CLARIFICATION" and user.id == ticket.author_id:
        set_status(db, ticket, user, "IN_REVIEW")
    if (
        user.role in ("MANAGER", "ADMIN")
        and user.id != ticket.author_id
        and ticket.first_response_at is None
    ):
        ticket.first_response_at = now()
    item = Comment(ticket_id=ticket_id, author_id=user.id, **data.model_dump())
    ticket.updated_at = now()
    db.add(item)
    if data.is_clarification_request:
        notify(db, ticket.author_id, ticket, user, "clarification", f"Запросили уточнение · {ticket.number}", short(data.body))
    else:
        title = f"Новый комментарий · {ticket.number}"
        notify(db, ticket.author_id, ticket, user, "comment", title, short(data.body))
        if ticket.assignee_id != ticket.author_id:
            notify(db, ticket.assignee_id, ticket, user, "comment", title, short(data.body))
    ticket_changed(db, ticket, "comment")
    db.commit()
    return {"id": item.id}


@router.post("/tickets/{ticket_id}/attachments", status_code=201)
async def upload(ticket_id: int, file: UploadFile, db: DB, user: CurrentUser):
    ticket = get_ticket(db, user, ticket_id)
    if ticket.status in ("CLOSED", "IMPLEMENTED", "REJECTED"):
        raise HTTPException(422, "Нельзя прикрепить файл к завершённому обращению")
    filename = Path((file.filename or "file").replace("\\", "/")).name
    extension = Path(filename).suffix.lower()
    types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".txt": "text/plain",
    }
    if extension not in types:
        raise HTTPException(422, "Разрешены pdf, png, jpg, docx, xlsx, txt")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Размер файла не должен превышать 10 МБ")
    UPLOADS.mkdir(exist_ok=True)
    path = UPLOADS / f"{uuid.uuid4().hex}{extension}"
    path.write_bytes(content)
    item = Attachment(
        ticket_id=ticket_id,
        filename=filename,
        stored_path=str(path),
        content_type=types[extension],
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(item)
    ticket.updated_at = now()
    ticket_changed(db, ticket, "attachment")
    try:
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"id": item.id, "filename": item.filename}


@router.get("/attachments/{attachment_id}")
def download(attachment_id: int, db: DB, user: CurrentUser):
    item = db.get(Attachment, attachment_id)
    if not item:
        raise HTTPException(404, "Файл не найден")
    get_ticket(db, user, item.ticket_id)
    if not Path(item.stored_path).is_file():
        raise HTTPException(404, "Файл отсутствует в хранилище")
    return FileResponse(
        item.stored_path, filename=item.filename, media_type=item.content_type
    )
