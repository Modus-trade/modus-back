"""Подсказки без ИИ: тип и категория по правилам, поиск похожих обращений.

Работают всегда, в том числе при локальном запуске без AI_URL.
"""
import re
from difflib import SequenceMatcher

from sqlalchemy import select

from ..models import Category, Ticket
from .deduplication import tokens

PRAISE = re.compile(r"спасибо|благодар|молодц|отличн\w* работ|признател|хочу отметить|понравил", re.I)
PROPOSAL = re.compile(
    r"предлага|предложени|было бы (удобно|хорошо|полезно|здорово)|можно (ли )?(сделать|добавить|ввести|завести)|"
    r"давайте|стоит (сделать|добавить|ввести)|хотелось бы|идея|нужно (сделать|добавить|ввести)|добавить",
    re.I,
)

# Слова, по которым видна категория. Ключ — название категории в базе.
KEYWORDS = {
    "Межотдельное взаимодействие": "отдел смежн между отдел передач заявк согласован закупк продаж взаимодейств",
    "Информационная прозрачность": "статус видн непонятн информаци узнать где сейчас прозрачн доступ сведени",
    "Каталог и данные": "каталог номенклатур артикул обозначени справочник данн изделие позици карточк товар",
    "Обучение": "обучени тренинг курс вебинар продукт новинк учеб занят",
    "Документооборот": "документ подпис согласован маршрут счет акт накладн бухгалтер оплат эдо",
    "Регламенты": "регламент правил инструкци порядок процесс памятк стандарт",
    "Интеграции и IT": "1с битрикс интеграц синхрониз систем программ сайт ошибк баг сервер доступ пароль компьютер принтер ноутбук сеть интернет почт",
    "Знания и наставничество": "новичк новых сотрудник наставник адаптац база знаний опыт передач знани",
    "Договорная работа": "договор контракт приложени юрист спецификац подписан тендер",
    "Задачи и коммуникации": "задач поручени чат переписк договорил встреч планерк созвон",
    "Оперативность": "срок долго ждать быстр задерж ответ скорост опаздыва затягива",
    "Коммуникации": "связ телефон мессенджер рассылк уведомлен канал общени корпоративн чат",
    "Проектная деятельность": "проект проектирован чертеж схем спецификац изменени версия",
    "Планирование ресурсов": "загрузк ресурс график планирован людей нехватк перегруз отпуск",
    "Постановка задач": "постановк тз техническ задани шаблон требовани формулировк",
    "Корпоративная среда": "офис кухн перерыв мебел кондиционер парковк корпоратив атмосфер уборк шум",
    "Хранение документов": "хранени архив папк файл диск облак актуальн версии найти документ",
    "Прозрачность результатов": "результат итог решени отчет реализова принят что сделано",
}


def _text(title: str, description: str) -> str:
    return f"{title}\n{description}"


def _score(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, len(a | b)) if a and b else 0.0


def rule_type(title: str, description: str) -> str:
    text = _text(title, description)
    if PRAISE.search(text):
        return "PRAISE"
    if PROPOSAL.search(text):
        return "PROPOSAL"
    return "PROBLEM"


def rule_category(db, title: str, description: str) -> tuple[int | None, float]:
    """Категория по словам и по похожим уже размеченным обращениям."""
    words = tokens(_text(title, description))
    if not words:
        return None, 0.0
    categories = db.scalars(select(Category).where(Category.is_active.is_(True))).all()
    scores = {c.id: 0.0 for c in categories}
    for c in categories:
        keys = {w for w in KEYWORDS.get(c.name, "").split()}
        hits = sum(any(word.startswith(k) or k.startswith(word) for k in keys) for word in words)
        scores[c.id] += min(hits, 4) * 0.12
    # Ближайшие обращения: их категорию проставил человек.
    for t in db.scalars(select(Ticket).order_by(Ticket.id.desc()).limit(500)):
        if t.category_id in scores:
            s = _score(words, tokens(_text(t.title, t.description)))
            scores[t.category_id] = max(scores[t.category_id], scores[t.category_id] * 0.5 + s * 1.5)
    best = max(scores, key=scores.get, default=None)
    if best is None or scores[best] <= 0:
        return None, 0.0
    return best, round(min(0.6, scores[best]), 2)


def similar_tickets(db, base_query, ticket: Ticket, limit: int = 5) -> list[tuple[Ticket, float]]:
    """Похожие обращения среди доступных пользователю (base_query = visible(user))."""
    words = tokens(_text(ticket.title, ticket.description))
    found = []
    for other in db.scalars(base_query.where(Ticket.id != ticket.id)):
        body = tokens(_text(other.title, other.description))
        score = 0.7 * _score(words, body) + 0.3 * SequenceMatcher(None, ticket.title.lower(), other.title.lower()).ratio()
        if other.category_id == ticket.category_id:
            score += 0.05
        if score >= 0.18:
            found.append((other, round(score, 2)))
    found.sort(key=lambda x: -x[1])
    return found[:limit]
