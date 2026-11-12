"""Подсказки: тип и категория при подаче, похожие обращения и вариант решения для ответственного."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..models import Category, Ticket, User
from ..security import CurrentUser, DB
from ..services import ai
from ..services.hints import rule_category, rule_type, similar_tickets
from ..services.tickets import get_ticket, require_manager, visible

router = APIRouter(prefix="/api", tags=["assist"])

FINAL = ("IMPLEMENTED", "REJECTED", "CLOSED")


class SuggestQuery(BaseModel):
    title: str = Field(default="", max_length=250)
    description: str = Field(default="", max_length=20000)
    image_base64: str | None = Field(default=None, max_length=7_000_000)

    @field_validator("title", "description")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


def category_info(db, category_id: int | None):
    category = db.get(Category, category_id) if category_id else None
    if not category or not category.is_active:
        return None
    assignee = db.get(User, category.default_assignee_id) if category.default_assignee_id else None
    return {
        "category_id": category.id,
        "category": category.name,
        "assignee_name": assignee.full_name if assignee and assignee.is_active else None,
        "sla_hours": category.sla_first_response_hours,
    }


@router.post("/assist/suggest")
def suggest(data: SuggestQuery, db: DB, user: CurrentUser):
    """Тип и категория по тексту (и фото). Сначала ИИ, если недоступен — правила."""
    if len(data.title) + len(data.description) < 12:
        raise HTTPException(422, "Опишите обращение подробнее")
    result = ai.classify(data.title, data.description, data.image_base64)
    if result and (info := category_info(db, result["category_id"])):
        return {**info, "type": result["type"], "confidence": result["confidence"],
                "reason": result["reason"], "source": "ai"}
    category_id, confidence = rule_category(db, data.title, data.description)
    info = category_info(db, category_id)
    if not info:
        return {"type": rule_type(data.title, data.description), "category_id": None, "category": None,
                "assignee_name": None, "sla_hours": None, "confidence": 0, "reason": "", "source": "rules"}
    return {**info, "type": rule_type(data.title, data.description), "confidence": confidence,
            "reason": "", "source": "rules"}


class VoiceQuery(BaseModel):
    audio_base64: str = Field(min_length=100, max_length=12_000_000)
    format: str = Field(default="m4a", pattern=r"^(m4a|mp4|aac|wav|webm|ogg|mp3|caf)$")


@router.post("/assist/voice")
def voice(data: VoiceQuery, db: DB, user: CurrentUser):
    """Обращение голосом: ИИ распознаёт речь, пишет тему и описание и подбирает тип и категорию.

    Сотрудник потом сам проверяет и правит поля — ничего не отправляется автоматически.
    """
    result = ai.voice(data.audio_base64, data.format)
    if not result:
        raise HTTPException(503, "Голосовой ввод сейчас недоступен — продиктуйте через микрофон на клавиатуре")
    info = category_info(db, result["category_id"]) if result["type"] else None
    if info:
        suggestion = {**info, "type": result["type"], "confidence": 0.8, "reason": result["reason"], "source": "ai"}
    else:
        suggestion = suggest(SuggestQuery(title=result["title"], description=result["description"]), db, user)
    return {
        "transcript": result["transcript"],
        "title": result["title"],
        "description": result["description"],
        "suggestion": suggestion,
    }


def similar_items(db, user, ticket: Ticket):
    items = []
    for other, score in similar_tickets(db, visible(user), ticket):
        category = db.get(Category, other.category_id)
        items.append({
            "id": other.id, "number": other.number, "title": other.title, "status": other.status,
            "category": category.name if category else "", "resolution": other.resolution,
            "created_at": other.created_at, "score": score,
        })
    return items


@router.get("/tickets/{ticket_id}/similar")
def similar(ticket_id: int, db: DB, user: CurrentUser):
    """Похожие обращения — только для ответственных и администраторов, только из доступных им."""
    require_manager(user)
    ticket = get_ticket(db, user, ticket_id)
    return {"items": similar_items(db, user, ticket), "ai_enabled": ai.enabled()}


@router.post("/tickets/{ticket_id}/solve")
def solve(ticket_id: int, db: DB, user: CurrentUser):
    """Вариант решения от ИИ с опорой на похожие обращения."""
    require_manager(user)
    ticket = get_ticket(db, user, ticket_id)
    items = similar_items(db, user, ticket)
    category = db.get(Category, ticket.category_id)
    result = ai.solve(
        {"type": ticket.type, "category": category.name if category else "", "title": ticket.title,
         "description": ticket.description, "proposed_solution": ticket.proposed_solution},
        [{"number": i["number"], "title": i["title"], "status": i["status"], "resolution": i["resolution"]}
         for i in items],
    )
    if result:
        return {**result, "source": "ai"}
    # Без ИИ: показываем, как решали самые похожие завершённые обращения.
    solved = [i for i in items if i["status"] in FINAL and i["resolution"]]
    if not solved:
        raise HTTPException(503, "ИИ сейчас недоступен, а похожих решённых обращений нет")
    return {
        "summary": "ИИ сейчас недоступен. Вот как решали похожие обращения:",
        "steps": [f"{i['number']}: {i['resolution'][:300]}" for i in solved[:3]],
        "based_on": [i["number"] for i in solved[:3]],
        "reply_draft": "",
        "source": "rules",
    }
