from datetime import timedelta
from pathlib import Path
import uuid
from typing import Annotated
from pydantic import BeforeValidator
import base64
from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select, text
from ..services.notifications import notify
from ..services.problems import affected_count
from ..services.deadlines import ACTIVE_STATUSES, set_resolution_deadline
from ..services.resolution_reviews import review_resolution
from ..database import BASE_DIR
from ..models import Attachment, Category, Comment, KnowledgeAttempt, ResolutionReview, Ticket, TicketHistory, User, now
from ..schemas import CommentCreate, ResolutionDeadlinePatch, ResolutionReviewCreate, TicketCreate, TicketPatch
from ..security import CurrentUser, DB
from ..services.live import ticket_changed
from ..services import ai
from ..services.tickets import (
    change,
    get_ticket,
    require_manager,
    set_status,
    validate_assignee,
    eligible_assignee_ids,
    joined_ticket,
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
    from ..models import TicketParticipant
    db.info["affected_counts"] = dict(db.execute(
        select(TicketParticipant.ticket_id, func.count())
        .join(Ticket, Ticket.id == TicketParticipant.ticket_id)
        .where(TicketParticipant.user_id != Ticket.author_id)
        .group_by(TicketParticipant.ticket_id)
    ).all())


def serialize(db, ticket):
    data = jsonable_encoder(ticket)
    import json
    data["onec_item"] = json.loads(ticket.onec_item) if ticket.onec_item else None
    data["onec_document"] = json.loads(ticket.onec_document) if ticket.onec_document else None
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
        affected_count=(1 + db.info["affected_counts"].get(ticket.id, 0)) if "affected_counts" in db.info else db.scalar(select(affected_count()).where(Ticket.id == ticket.id)),
        overdue=ticket.first_response_at is None and now() > ticket.due_at,
        resolution_due_at=ticket.resolution_due_at.isoformat() if ticket.resolution_due_at else None,
        resolution_due_version=ticket.resolution_due_version,
        resolution_review_version=ticket.resolution_review_version,
        resolution_confirmed_at=ticket.resolution_confirmed_at.isoformat() if ticket.resolution_confirmed_at else None,
        resolution_overdue=(ticket.status in ACTIVE_STATUSES and ticket.resolution_due_at is not None
                            and ticket.resolution_due_at <= now()),
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
        active_assignees = eligible_assignee_ids(db)
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
    if sort == "affected_count":
        column = affected_count()
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
    if data.is_shared and data.type != "PROBLEM":
        raise HTTPException(422, "Присоединение доступно только к проблемам")
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
        if assignee and assignee.id in eligible_assignee_ids(db)
        else None
    )
    # Номер обращения совпадает с его id: A-001, A-002…
    ticket_id = next_ticket_id(db)
    attempt = None
    if data.self_service_attempt_id is not None:
        attempt = db.scalar(select(KnowledgeAttempt).where(
            KnowledgeAttempt.id == str(data.self_service_attempt_id), KnowledgeAttempt.user_id == user.id,
        ).with_for_update())
        if attempt is None:
            raise HTTPException(404, "Попытка поиска решения не найдена")
        if attempt.ticket_id is not None:
            return serialize(db, get_ticket(db, user, attempt.ticket_id))
        if attempt.joined_problem_id is not None:
            raise HTTPException(409, "Вы уже присоединились к общей проблеме. Откройте новую форму.")
    from ..services import onec
    from ..services.onec_client import OneCError
    import json
    try:
        item = onec.snapshot(db,'items',data.onec_item_ref,data.onec_scope) if data.onec_item_ref else None
        document = None
        if data.onec_document_ref:
            if data.is_shared:
                raise HTTPException(422, "Обращение с документом 1С должно быть приватным")
            if not onec.allow_documents(user,onec.settings(db).mode):
                raise HTTPException(403, "Нет доступа к документам 1С")
            document=onec.snapshot(db,data.onec_document_kind,data.onec_document_ref,data.onec_scope)
    except OneCError as e:
        raise HTTPException(422,str(e))
    ticket = Ticket(
        onec_item=json.dumps(item,ensure_ascii=False) if item else None,
        onec_document=json.dumps(document,ensure_ascii=False) if document else None,
        **data.model_dump(exclude={"self_service_attempt_id", "onec_item_ref", "onec_document_ref", "onec_document_kind", "onec_scope"}),
        id=ticket_id,
        number=f"A-{ticket_id:03d}",
        author_id=user.id,
        assignee_id=assignee_id,
        due_at=now() + timedelta(hours=category.sla_first_response_hours),
    )
    db.add(ticket)
    db.flush()
    if attempt is not None:
        attempt.ticket_id = ticket.id
        attempt.resolved_at = None
        attempt.resolved_article_id = None
        attempt.resolved_version = None
    notify(db, ticket, user.id, "assigned" if assignee_id else "created")
    ticket_changed(db, ticket, "created")
    db.commit()
    return serialize(db, ticket)


@router.get("/tickets/{ticket_id}")
def detail(ticket_id: int, db: DB, user: CurrentUser):
    preload(db)
    member = db.scalar(visible(user).where(Ticket.id == ticket_id))
    ticket = member or joined_ticket(db, user, ticket_id)
    if not ticket:
        raise HTTPException(404, "Обращение не найдено")
    result = serialize(db, ticket)
    # Присоединившийся к общей проблеме видит заголовок, стадию, ответственного и результат.
    # Описание, автор, переписка и вложения остаются приватными.
    result["viewer"] = "member" if member else "participant"
    if result["viewer"] == "participant":
        result.update(description="", proposed_solution=None, author_name="", author_id=0, department="",
                      comments=[], history=[], attachments=[], resolution_reviews=[])
        return result
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
    voice_ids = {c["audio_id"] for c in result["comments"] if c.get("audio_id")}
    result["attachments"] = [
        {k: v for k, v in jsonable_encoder(a).items() if k != "stored_path"}
        for a in db.scalars(select(Attachment).where(Attachment.ticket_id == ticket_id))
        if a.id not in voice_ids
    ]
    result["resolution_reviews"] = [
        jsonable_encoder(review)
        for review in db.scalars(select(ResolutionReview)
                                 .where(ResolutionReview.ticket_id == ticket_id)
                                 .order_by(ResolutionReview.version.desc()))
    ]
    return result


@router.patch("/tickets/{ticket_id}")
def patch_ticket(ticket_id: int, data: TicketPatch, db: DB, user: CurrentUser):
    if user.role == "EMPLOYEE" and db.scalar(select(Ticket.id).where(Ticket.id == ticket_id, Ticket.assignee_id == user.id)) is None:
        raise HTTPException(403, "Недостаточно прав")
    ticket = get_ticket(db, user, ticket_id, lock=True)
    require_manager(user, ticket)
    old_status, old_assignee, old_resolution = ticket.status, ticket.assignee_id, ticket.resolution
    values = data.model_dump(exclude_unset=True)
    expected_status = values.pop("expected_status", None)
    expected_review_version = values.pop("expected_resolution_review_version", None)
    if ((expected_status is not None and expected_status != ticket.status)
            or (expected_review_version is not None and expected_review_version != ticket.resolution_review_version)):
        raise HTTPException(409, "Обращение изменилось. Обновите карточку перед сохранением.")
    if ticket.status == "CLOSED" and values:
        raise HTTPException(422, "Закрытое обращение нельзя изменять")
    if values.get("is_shared") and ticket.onec_document:
        raise HTTPException(422, "Обращение с документом 1С нельзя сделать общим")
    if "is_shared" in values:
        if values["is_shared"] is None or (values["is_shared"] and ticket.type != "PROBLEM"):
            raise HTTPException(422, "Общей может быть только проблема")
        if ticket.is_shared and values["is_shared"] is False:
            raise HTTPException(422, "Общую проблему нельзя скрыть от присоединившихся")
    if any(v is None for k, v in values.items() if k in ("status", "priority")):
        raise HTTPException(422, "Статус и приоритет не могут быть пустыми")
    if "assignee_id" in values:
        validate_assignee(db, values["assignee_id"])
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
    if ticket.status == "IMPLEMENTED" and (old_status != "IMPLEMENTED" or old_resolution != ticket.resolution):
        ticket.resolution_review_version += 1
        ticket.resolution_confirmed_at = None
    if old_assignee != ticket.assignee_id:
        notify(db, ticket, user.id, "assigned")
    if ticket.resolution and ticket.status in ("IMPLEMENTED", "REJECTED", "CLOSED") and (old_resolution != ticket.resolution or old_status != ticket.status):
        notify(db, ticket, user.id, "result")
    elif old_status != ticket.status:
        notify(db, ticket, user.id, "clarification" if ticket.status == "AWAITING_CLARIFICATION" else "status")
    ticket_changed(db, ticket, "updated")
    db.commit()
    return serialize(db, ticket)


@router.patch("/tickets/{ticket_id}/resolution-deadline")
def patch_resolution_deadline(ticket_id: int, data: ResolutionDeadlinePatch, db: DB, user: CurrentUser):
    if user.role == "EMPLOYEE" and db.scalar(select(Ticket.id).where(Ticket.id == ticket_id, Ticket.assignee_id == user.id)) is None:
        raise HTTPException(403, "Недостаточно прав")
    ticket = get_ticket(db, user, ticket_id, lock=True)
    require_manager(user, ticket)
    if set_resolution_deadline(db, ticket, user, data):
        notify(db, ticket, user.id, "deadline_changed")
        ticket_changed(db, ticket, "deadline_changed")
    db.commit()
    return serialize(db, ticket)


@router.post("/tickets/{ticket_id}/resolution-review")
def submit_resolution_review(ticket_id: int, data: ResolutionReviewCreate, db: DB, user: CurrentUser):
    ticket = get_ticket(db, user, ticket_id, lock=True)
    if review_resolution(db, ticket, user, data):
        kind = "resolution_confirmed" if data.decision == "confirm" else "resolution_rework"
        notify(db, ticket, user.id, kind)
        ticket_changed(db, ticket, kind)
    db.commit()
    return serialize(db, ticket)


def add_comment(db, ticket: Ticket, user: User, body: str, clarify: bool, **extra) -> Comment:
    if ticket.status in ("CLOSED", "IMPLEMENTED", "REJECTED"):
        raise HTTPException(422, "Обсуждение завершённого обращения закрыто")
    if clarify:
        require_manager(user, ticket)
        set_status(db, ticket, user, "AWAITING_CLARIFICATION")
    elif ticket.status == "AWAITING_CLARIFICATION" and user.id == ticket.author_id:
        set_status(db, ticket, user, "IN_REVIEW")
    if (
        (user.role in ("MANAGER", "ADMIN") or ticket.assignee_id == user.id)
        and user.id != ticket.author_id
        and ticket.first_response_at is None
    ):
        ticket.first_response_at = now()
    notify(db, ticket, user.id, "clarification" if clarify else "comment")
    item = Comment(ticket_id=ticket.id, author_id=user.id, body=body, is_clarification_request=clarify, **extra)
    ticket.updated_at = now()
    db.add(item)
    ticket_changed(db, ticket, "comment")
    return item


@router.post("/tickets/{ticket_id}/comments", status_code=201)
def comment(ticket_id: int, data: CommentCreate, db: DB, user: CurrentUser):
    ticket = get_ticket(db, user, ticket_id, lock=True)
    if not data.body.strip():
        raise HTTPException(422, "Введите текст комментария")
    item = add_comment(db, ticket, user, data.body, data.is_clarification_request)
    db.commit()
    return {"id": item.id}


AUDIO = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac", ".webm": "audio/webm", ".ogg": "audio/ogg"}
VOICE_BODY = "🎤 Голосовое сообщение"


def transcribe_comment(comment_id: int):
    """Фоном: расшифровка от ИИ. Пришла — все, кто открыл обращение, увидят текст сразу."""
    from ..database import SessionLocal
    with SessionLocal() as db:
        item = db.get(Comment, comment_id)
        audio = db.get(Attachment, item.audio_id) if item and item.audio_id else None
        if not audio or not Path(audio.stored_path).is_file():
            return
        raw = Path(audio.stored_path).read_bytes()
        text = ai.transcribe(base64.b64encode(raw).decode(), Path(audio.stored_path).suffix.lstrip(".") or "m4a")
        item.transcript = text if text else ""
        ticket_changed(db, db.get(Ticket, item.ticket_id), "comment")
        db.commit()


@router.post("/tickets/{ticket_id}/voice", status_code=201)
async def voice_comment(
    ticket_id: int,
    file: UploadFile,
    background: BackgroundTasks,
    db: DB,
    user: CurrentUser,
    seconds: int = Form(0, ge=0, le=600),
    is_clarification_request: bool = Form(False),
):
    """Голосовое сообщение в обсуждении: запись сохраняется сразу, расшифровка догоняет."""
    ticket = get_ticket(db, user, ticket_id, lock=True)
    extension = Path((file.filename or "voice.m4a").replace("\\", "/")).suffix.lower() or ".m4a"
    if extension not in AUDIO:
        raise HTTPException(422, "Поддерживаются записи m4a, webm, ogg")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "Запись слишком длинная")
    if len(content) < 500:
        raise HTTPException(422, "Запись пустая")
    UPLOADS.mkdir(exist_ok=True)
    path = UPLOADS / f"{uuid.uuid4().hex}{extension}"
    path.write_bytes(content)
    audio = Attachment(ticket_id=ticket.id, filename=f"Голосовое{extension}", stored_path=str(path),
                       content_type=AUDIO[extension], size_bytes=len(content), uploaded_by_id=user.id)
    db.add(audio)
    try:
        db.flush()
        item = add_comment(db, ticket, user, VOICE_BODY, is_clarification_request,
                           audio_id=audio.id, audio_seconds=seconds or None,
                           transcript=None if ai.enabled() else "")
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if ai.enabled():
        background.add_task(transcribe_comment, item.id)
    return {"id": item.id}


@router.post("/tickets/{ticket_id}/attachments", status_code=201)
async def upload(ticket_id: int, file: UploadFile, db: DB, user: CurrentUser):
    ticket = get_ticket(db, user, ticket_id, lock=True)
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
