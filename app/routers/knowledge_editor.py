from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from ..models import KnowledgeArticle, KnowledgeDraft, KnowledgeReport, KnowledgeRevision, KnowledgeAudit, Ticket, User, now
from ..security import CurrentUser, DB
from ..services.knowledge import audit, changed, findings, notify_admins
from ..services.tickets import get_ticket, require_manager, visible
from .knowledge import no_store

router = APIRouter(prefix="/api", tags=["knowledge editing"], dependencies=[Depends(no_store)])


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=250)
    summary: str = Field(max_length=600)
    body: str = Field(max_length=15000)
    visibility: Literal["COMPANY", "DEPARTMENT"] = "DEPARTMENT"
    review_days: int = Field(default=90, ge=7, le=365)
    expected_version: int = Field(ge=0)
    confidential_case: bool = False

    @field_validator("title", "summary", "body", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class ReviewAction(BaseModel):
    expected_version: int = Field(ge=1)
    reviewed: Literal[True]
    warnings_reviewed: bool = False


class WithdrawAction(BaseModel):
    expected_version: int = Field(ge=1)


def context(db, user, ticket_id, *, lock=False):
    require_manager(user)
    ticket = get_ticket(db, user, ticket_id, lock=lock)
    article = db.scalar(select(KnowledgeArticle).where(KnowledgeArticle.source_ticket_id == ticket_id))
    draft = db.get(KnowledgeDraft, article.id) if article else None
    return ticket, article, draft


def require_current(draft, expected):
    if draft is None or draft.version != expected:
        raise HTTPException(409, "Черновик изменился. Загрузите актуальную версию.")


def require_reviewed(draft, data):
    if draft.confidential_case:
        raise HTTPException(422, "Материал отмечен как конфиденциальный случай. Подготовьте обобщённую инструкцию перед публикацией.")
    if len(draft.title) < 3 or len(draft.summary) < 10 or len(draft.body) < 20:
        raise HTTPException(422, "Заполните название, условия применения и порядок действий")
    risks = findings(draft.title, draft.summary, draft.body)
    if any(risk["severity"] == "block" for risk in risks):
        raise HTTPException(422, "В тексте обнаружены возможные секреты. Удалите значения паролей и токенов.")
    if risks and not data.warnings_reviewed:
        raise HTTPException(422, "Проверьте отмеченные данные и подтвердите их допустимость для выбранной аудитории")


def can_publish(user, draft):
    if user.role == "ADMIN":
        return True
    return draft.visibility == "DEPARTMENT" and user.department == draft.owner_department


def managed(db, user, ticket, article, draft):
    if article is None:
        return None
    versions = db.scalars(select(KnowledgeRevision).where(KnowledgeRevision.article_id == article.id)
                          .order_by(KnowledgeRevision.version.desc())).all()
    history_query = select(KnowledgeAudit).where(KnowledgeAudit.article_id == article.id)
    if user.role != "ADMIN":
        history_query = history_query.where(KnowledgeAudit.action != "reported")
    history = db.scalars(history_query.order_by(KnowledgeAudit.id.desc()).limit(30)).all()
    reports = db.scalars(select(KnowledgeReport).where(KnowledgeReport.article_id == article.id,
                                                     KnowledgeReport.is_open)).all()
    return {
        "id": article.id, "source_ticket_id": ticket.id,
        "published_version": article.version, "is_published": article.is_published,
        "review_due_at": article.review_due_at,
        "draft": {"title": draft.title, "summary": draft.summary, "body": draft.body,
                  "visibility": draft.visibility, "owner_department": draft.owner_department,
                  "confidential_case": draft.confidential_case,
                  "version": draft.version, "status": draft.status, "review_days": draft.review_days,
                  "updated_at": draft.updated_at},
        "can_publish": can_publish(user, draft),
        "findings": findings(draft.title, draft.summary, draft.body),
        "revisions": [{"version": revision.version, "title": revision.title,
                       "reviewer": db.get(User, revision.reviewer_id).full_name,
                       "approved_at": revision.approved_at} for revision in versions],
        "history": [{"id": event.id, "action": event.action,
                     "actor": db.get(User, event.actor_id).full_name, "created_at": event.created_at}
                    for event in history],
        "open_reports": len(reports),
        "reports": [{"id": report.id, "reason": report.reason, "version": report.version,
                     "created_at": report.created_at} for report in reports] if user.role == "ADMIN" else [],
    }


@router.get("/tickets/{ticket_id}/knowledge-article")
def get_managed(ticket_id: int, db: DB, user: CurrentUser):
    return managed(db, user, *context(db, user, ticket_id))


@router.put("/tickets/{ticket_id}/knowledge-article")
def save_draft(ticket_id: int, data: DraftEdit, db: DB, user: CurrentUser):
    ticket, article, draft = context(db, user, ticket_id, lock=True)
    if ticket.status != "CLOSED" or ticket.resolution_confirmed_at is None or ticket.type == "PRAISE":
        raise HTTPException(422, "Подготовка инструкции доступна после подтверждения решения автором")
    timestamp = now()
    if article is None:
        if data.expected_version != 0:
            raise HTTPException(409, "Загрузите актуальный черновик")
        owner = db.get(User, ticket.author_id).department
        article = KnowledgeArticle(source_ticket_id=ticket.id, category_id=ticket.category_id,
                                   title="", summary="", body="", visibility="DEPARTMENT", department=owner,
                                   is_published=False, version=0, editor_id=user.id,
                                   updated_at=timestamp, review_due_at=timestamp)
        db.add(article)
        db.flush()
        draft = KnowledgeDraft(article_id=article.id, owner_department=owner, version=1)
        db.add(draft)
    else:
        require_current(draft, data.expected_version)
        draft.version += 1
    for field in ("title", "summary", "body", "visibility", "review_days", "confidential_case"):
        setattr(draft, field, getattr(data, field))
    draft.status, draft.editor_id, draft.updated_at = "DRAFT", user.id, timestamp
    audit(db, article, user, "draft_saved")
    db.commit()
    return managed(db, user, ticket, article, draft)


@router.post("/tickets/{ticket_id}/knowledge-article/submit")
def submit(ticket_id: int, data: ReviewAction, db: DB, user: CurrentUser):
    ticket, article, draft = context(db, user, ticket_id, lock=True)
    require_current(draft, data.expected_version)
    require_reviewed(draft, data)
    if draft.status != "DRAFT":
        raise HTTPException(409, "Для проверки сначала сохраните изменения в черновике")
    draft.status, draft.updated_at = "IN_REVIEW", now()
    draft.version += 1
    audit(db, article, user, "submitted")
    notify_admins(db, ticket, user.id, "knowledge_review")
    changed(db)
    db.commit()
    return managed(db, user, ticket, article, draft)


@router.post("/tickets/{ticket_id}/knowledge-article/approve")
def approve(ticket_id: int, data: ReviewAction, db: DB, user: CurrentUser):
    ticket, article, draft = context(db, user, ticket_id, lock=True)
    require_current(draft, data.expected_version)
    if not can_publish(user, draft):
        raise HTTPException(403, "Публикацию для всей компании или другого подразделения подтверждает администратор")
    if draft.status != "IN_REVIEW":
        raise HTTPException(409, "Отправьте актуальный черновик на проверку")
    require_reviewed(draft, data)
    timestamp = now()
    for field in ("title", "summary", "body", "visibility"):
        setattr(article, field, getattr(draft, field))
    article.department = draft.owner_department if draft.visibility == "DEPARTMENT" else None
    article.version += 1
    article.editor_id, article.updated_at, article.is_published = draft.editor_id, timestamp, True
    article.review_due_at = timestamp + timedelta(days=draft.review_days)
    db.add(KnowledgeRevision(article_id=article.id, version=article.version, title=article.title,
                             summary=article.summary, body=article.body, visibility=article.visibility,
                             department=article.department, editor_id=draft.editor_id, reviewer_id=user.id,
                             approved_at=timestamp, review_due_at=article.review_due_at))
    draft.status, draft.updated_at = "PUBLISHED", timestamp
    draft.version += 1
    audit(db, article, user, "published")
    changed(db)
    db.commit()
    return managed(db, user, ticket, article, draft)


@router.post("/tickets/{ticket_id}/knowledge-article/withdraw")
def withdraw(ticket_id: int, data: WithdrawAction, db: DB, user: CurrentUser):
    ticket, article, draft = context(db, user, ticket_id, lock=True)
    if article is None or article.version != data.expected_version:
        raise HTTPException(409, "Публикация изменилась. Обновите данные.")
    if article.is_published:
        article.is_published = False
        db.query(KnowledgeReport).filter(KnowledgeReport.article_id == article.id).update({"is_open": False})
        audit(db, article, user, "withdrawn")
        changed(db)
        db.commit()
    return managed(db, user, ticket, article, draft)


@router.get("/knowledge/manage")
def management(db: DB, user: CurrentUser):
    require_manager(user)
    rows = db.execute(select(KnowledgeArticle, KnowledgeDraft, Ticket)
                      .join(KnowledgeDraft, KnowledgeDraft.article_id == KnowledgeArticle.id)
                      .join(Ticket, Ticket.id == KnowledgeArticle.source_ticket_id)
                      .where(Ticket.id.in_(visible(user).with_only_columns(Ticket.id)))
                      .order_by(KnowledgeDraft.updated_at.desc())).all()
    items = []
    for article, draft, ticket in rows:
        status = draft.status
        if status == "PUBLISHED":
            status = "PUBLISHED" if article.is_published and article.review_due_at > now() else "REVIEW_DUE"
        items.append({"id": article.id, "source_ticket_id": ticket.id, "title": draft.title or "Новая инструкция",
                      "summary": draft.summary, "status": status, "updated_at": draft.updated_at,
                      "audience": "Все сотрудники" if draft.visibility == "COMPANY" else draft.owner_department,
                      "open_reports": db.scalar(select(func.count()).select_from(KnowledgeReport).where(
                          KnowledgeReport.article_id == article.id, KnowledgeReport.is_open))})
    return {"items": items}


@router.get("/tickets/{ticket_id}/knowledge-article/revisions/{version}")
def revision(ticket_id: int, version: int, db: DB, user: CurrentUser):
    _, article, _ = context(db, user, ticket_id)
    row = db.scalar(select(KnowledgeRevision).where(KnowledgeRevision.article_id == article.id,
                                                   KnowledgeRevision.version == version)) if article else None
    if row is None:
        raise HTTPException(404, "Версия не найдена")
    return {"title": row.title, "summary": row.summary, "body": row.body, "version": row.version,
            "audience": row.department if row.visibility == "DEPARTMENT" else "Все сотрудники"}
