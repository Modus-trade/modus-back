import re
from sqlalchemy import select
from ..models import KnowledgeAudit, Notification, User
from ..realtime import queue
from .notifications import serialize


def findings(title, summary, body):
    patterns = (
        ("credential", "block", r"(?i)(?:пароль|password|api[_ -]?key|secret|токен)[\"']?\s*[:=]\s*[\"']?(?=[^\s\"']{6,})(?=[^\s\"']*[a-zа-я])(?=[^\s\"']*\d)[^\s\"']+|\bBearer\s+[\w.-]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:ghp_|sk-proj-)[\w-]{12,}", "Возможный пароль, токен или секрет. Удалите значение из инструкции."),
        ("credential_hint", "warning", r"(?i)(?:пароль|password|api[_ -]?key|secret|токен)[\"']?\s*[:=]", "Проверьте упоминание секрета. Указания пользователю должны содержать безопасные примеры."),
        ("email", "warning", r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "Проверьте адрес почты. Для инструкции предпочтителен общий рабочий контакт."),
        ("phone", "warning", r"(?<!\d)\+\d[\d ()-]{8,}\d", "Проверьте, разрешено ли публиковать этот телефон."),
        ("identifier", "warning", r"(?<!\d)\d{12,19}(?!\d)", "Возможный личный, банковский или служебный идентификатор. Проверьте необходимость публикации."),
        ("link", "warning", r"https?://\S+", "Проверьте ссылку, её аудиторию и параметры доступа."),
    )
    result = []
    for field, text in (("title", title), ("summary", summary), ("body", body)):
        for kind, severity, pattern, message in patterns:
            if re.search(pattern, text):
                result.append({"field": field, "kind": kind, "severity": severity, "message": message})
    return result


def audit(db, article, user, action):
    db.add(KnowledgeAudit(article_id=article.id, actor_id=user.id, action=action, version=article.version))


def changed(db):
    # No title, identifier or source information is broadcast to other audiences.
    users = set(db.scalars(select(User.id).where(User.is_active)))
    queue(db, users, {"type": "knowledge_changed"})


def notify_admins(db, ticket, actor_id, kind):
    users = db.scalars(select(User).where(User.role == "ADMIN", User.is_active, User.id != actor_id))
    for user in users:
        notice = Notification(user_id=user.id, ticket_id=ticket.id, kind=kind)
        db.add(notice)
        db.flush()
        queue(db, {user.id}, {"type": "notification", "notification": serialize(notice, True, ticket)})
