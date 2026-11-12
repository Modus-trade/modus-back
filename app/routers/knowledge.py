from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import Category, KnowledgeArticle, KnowledgeAttempt, KnowledgeExposure, KnowledgeReport, KnowledgeRevision, Ticket, now
from ..security import CurrentUser, DB
from ..services.text_matching import tokens
from ..services.tickets import require_manager
from ..services.knowledge import audit, changed, notify_admins

def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/api", tags=["knowledge"], dependencies=[Depends(no_store)])


class SuggestQuery(BaseModel):
    text: str = Field(min_length=1, max_length=20500)
    category_id: int | None = None


class ArticleView(BaseModel):
    attempt_id: UUID
    expected_version: int = Field(ge=1)


class ArticleFeedback(ArticleView):
    helped: bool


def published(user):
    audience = or_(
        KnowledgeArticle.visibility == "COMPANY",
        (KnowledgeArticle.visibility == "DEPARTMENT") & (KnowledgeArticle.department == user.department),
    )
    return select(KnowledgeArticle).join(KnowledgeRevision, (KnowledgeRevision.article_id == KnowledgeArticle.id) & (KnowledgeRevision.version == KnowledgeArticle.version)).where(
        KnowledgeArticle.is_published.is_(True), KnowledgeArticle.review_due_at > now(), audience,
    )


def public_article(db, article, *, full=False):
    revision = db.scalar(select(KnowledgeRevision).where(KnowledgeRevision.article_id == article.id,
                                                       KnowledgeRevision.version == article.version))
    result = {
        "id": article.id, "title": article.title, "summary": article.summary,
        "category_id": article.category_id, "category_name": db.get(Category, article.category_id).name,
        "version": article.version, "updated_at": article.updated_at,
        "review_due_at": article.review_due_at,
        "audience": "Все сотрудники" if article.visibility == "COMPANY" else article.department,
        "verified_at": revision.approved_at,
    }
    if full:
        result["body"] = article.body
    return result


def available_article(db, user, article_id, version=None, *, lock=False):
    query = published(user).where(KnowledgeArticle.id == article_id)
    article = db.scalar(query.with_for_update(of=KnowledgeArticle) if lock else query)
    if article is None:
        raise HTTPException(404, "Инструкция недоступна или ожидает проверки актуальности")
    if version is not None and article.version != version:
        raise HTTPException(409, "Инструкция обновилась. Откройте актуальную версию.")
    if lock and db.get_bind().dialect.name == "sqlite":
        changed = db.execute(update(KnowledgeArticle).where(
            KnowledgeArticle.id == article.id, KnowledgeArticle.version == article.version,
            KnowledgeArticle.is_published.is_(True), KnowledgeArticle.review_due_at > now(),
        ).values(version=KnowledgeArticle.version).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise HTTPException(409, "Инструкция изменена. Обновите список решений.")
    return article


def insert_for(db, model):
    return pg_insert(model) if db.get_bind().dialect.name == "postgresql" else sqlite_insert(model)


@router.get("/knowledge")
def articles(db: DB, user: CurrentUser, q: str = Query("", max_length=250), page: int = Query(1, ge=1), category_id: int | None = None):
    query = published(user)
    if category_id is not None:
        query = query.where(KnowledgeArticle.category_id == category_id)
    if q.strip():
        query = query.where(or_(
            KnowledgeArticle.title.icontains(q.strip(), autoescape=True),
            KnowledgeArticle.summary.icontains(q.strip(), autoescape=True),
            KnowledgeArticle.body.icontains(q.strip(), autoescape=True),
        ))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(KnowledgeArticle.updated_at.desc(), KnowledgeArticle.id.desc())
                       .offset((page - 1) * 20).limit(20)).all()
    return {"items": [public_article(db, item) for item in items], "total": total}


class CatalogSearch(BaseModel):
    q: str = Field(default="", max_length=250)
    page: int = Field(default=1, ge=1)
    category_id: int | None = None


@router.post("/knowledge/search")
def search_catalog(data: CatalogSearch, db: DB, user: CurrentUser):
    return articles(db, user, q=data.q, page=data.page, category_id=data.category_id)


class ReportData(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=2000)


@router.post("/knowledge/{article_id}/report")
def report(article_id: int, data: ReportData, db: DB, user: CurrentUser):
    if len(data.reason.strip()) < 10:
        raise HTTPException(422, "Опишите, какие сведения требуют проверки")
    article = available_article(db, user, article_id, data.expected_version, lock=True)
    inserted = db.scalar(insert_for(db, KnowledgeReport).values(
        article_id=article.id, version=article.version, reporter_id=user.id, reason=data.reason.strip(),
        is_open=True, created_at=now(),
    ).on_conflict_do_nothing(index_elements=["article_id", "version", "reporter_id"])
        .returning(KnowledgeReport.id))
    if inserted is not None:
        audit(db, article, user, "reported")
        notify_admins(db, db.get(Ticket, article.source_ticket_id), user.id, "knowledge_report")
        changed(db)
    db.commit()
    return {"reported": True}


@router.post("/knowledge/suggest")
def suggest(data: SuggestQuery, db: DB, user: CurrentUser):
    words = tokens(data.text)
    if len(words) < 2:
        return {"items": []}
    # Search exclusively the text explicitly reviewed for publication.
    terms = sorted(words)[:16]
    query = published(user).where(or_(*[
        field.icontains(word, autoescape=True)
        for word in terms for field in (KnowledgeArticle.title, KnowledgeArticle.summary, KnowledgeArticle.body)
    ]))
    ranked = []
    for article in db.scalars(query):
        heading = tokens(article.title + " " + article.summary)
        content = tokens(article.body)
        common = words & (heading | content)
        if len(common) < 2:
            continue
        score = (.7 * len(words & heading) / max(1, min(len(words), len(heading)))
                 + .3 * len(words & content) / max(1, min(len(words), len(content))))
        if score >= .35:
            ranked.append((score + (.03 if article.category_id == data.category_id else 0), article))
    ranked.sort(key=lambda item: (-item[0], -item[1].id))
    return {"items": [public_article(db, article) for _, article in ranked[:3]]}


@router.get("/knowledge/metrics")
def metrics(db: DB, user: CurrentUser, days: int = Query(30, ge=1, le=365)):
    require_manager(user)
    query = select(KnowledgeAttempt).where(KnowledgeAttempt.created_at >= now() - timedelta(days=days))
    if user.role != "ADMIN":
        query = query.where(KnowledgeAttempt.department == user.department)
    attempts = db.scalars(query).all()
    resolved = sum(item.resolved_at is not None and item.ticket_id is None and item.joined_problem_id is None for item in attempts)
    submitted = sum(item.ticket_id is not None for item in attempts)
    joined = sum(item.joined_problem_id is not None for item in attempts)
    total = len(attempts)
    return {
        "days": days, "viewed_attempts": total, "resolved_attempts": resolved,
        "submitted_attempts": submitted, "joined_attempts": joined,
        "pending_attempts": total - resolved - submitted - joined,
        "resolved_percent": round(resolved / total * 100, 1) if total else None,
        "scope": "company" if user.role == "ADMIN" else "department",
    }


@router.get("/knowledge/{article_id}")
def article_detail(article_id: int, db: DB, user: CurrentUser):
    return public_article(db, available_article(db, user, article_id), full=True)


@router.post("/knowledge/{article_id}/view")
def view_article(article_id: int, data: ArticleView, db: DB, user: CurrentUser):
    article = available_article(db, user, article_id, data.expected_version, lock=True)
    attempt_id = str(data.attempt_id)
    db.execute(insert_for(db, KnowledgeAttempt).values(
        id=attempt_id, user_id=user.id, department=user.department, created_at=now(),
    ).on_conflict_do_nothing(index_elements=["id"]))
    attempt = db.scalar(select(KnowledgeAttempt).where(
        KnowledgeAttempt.id == attempt_id, KnowledgeAttempt.user_id == user.id,
    ).with_for_update())
    if attempt is None:
        raise HTTPException(404, "Попытка не найдена")
    if attempt.ticket_id is not None or attempt.joined_problem_id is not None:
        raise HTTPException(409, "Обращение уже отправлено. Откройте новую форму.")
    db.execute(insert_for(db, KnowledgeExposure).values(
        attempt_id=attempt_id, article_id=article.id, version=article.version, created_at=now(),
    ).on_conflict_do_nothing(index_elements=["attempt_id", "article_id", "version"]))
    exposure = db.get(KnowledgeExposure, (attempt_id, article.id, article.version))
    result = {**public_article(db, article, full=True), "helped": exposure.helped}
    db.commit()
    return result


@router.post("/knowledge/{article_id}/feedback")
def feedback(article_id: int, data: ArticleFeedback, db: DB, user: CurrentUser):
    article = available_article(db, user, article_id, data.expected_version, lock=True)
    attempt_id = str(data.attempt_id)
    attempt = db.scalar(select(KnowledgeAttempt).where(
        KnowledgeAttempt.id == attempt_id, KnowledgeAttempt.user_id == user.id,
    ).with_for_update())
    exposure = db.get(KnowledgeExposure, (attempt_id, article.id, article.version))
    if attempt is None or exposure is None:
        raise HTTPException(404, "Сначала откройте инструкцию в форме обращения")
    if attempt.ticket_id is not None or attempt.joined_problem_id is not None:
        raise HTTPException(409, "По этой попытке уже создано обращение")
    values = {}
    if data.helped:
        values = {"resolved_article_id": article.id, "resolved_version": article.version, "resolved_at": now()}
    else:
        matches = ((KnowledgeAttempt.resolved_article_id == article.id)
                   & (KnowledgeAttempt.resolved_version == article.version))
        values = {
            "resolved_article_id": case((matches, None), else_=KnowledgeAttempt.resolved_article_id),
            "resolved_version": case((matches, None), else_=KnowledgeAttempt.resolved_version),
            "resolved_at": case((matches, None), else_=KnowledgeAttempt.resolved_at),
        }
    # A conditional update also serializes feedback with ticket creation on SQLite.
    changed = db.execute(update(KnowledgeAttempt).where(
        KnowledgeAttempt.id == attempt_id, KnowledgeAttempt.ticket_id.is_(None),
        KnowledgeAttempt.joined_problem_id.is_(None),
    ).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(409, "Обращение уже отправлено")
    exposure.helped = data.helped
    db.commit()
    return {"helped": data.helped}
