"""Add two unassigned demo tickets without modifying existing tickets. Safe to rerun."""
import sys
sys.stdout.reconfigure(encoding="utf-8")  # Windows: русский вывод не падает в консоли cp1252/cp866
from datetime import timedelta
from sqlalchemy import select, text
from app.database import SessionLocal
from app.models import Category, Ticket, TicketHistory, User, now


def seed():
    with SessionLocal() as db:
        postgres = db.get_bind().dialect.name == "postgresql"
        if postgres:
            db.execute(text("LOCK TABLE tickets IN SHARE ROW EXCLUSIVE MODE"))
        else:
            db.execute(text("BEGIN IMMEDIATE"))
        author = db.scalar(select(User).where(User.email == "employee@modus.demo"))
        admin = db.scalar(select(User).where(User.email == "admin@modus.demo"))
        if not author or not admin:
            raise RuntimeError("Сначала загрузите основной демодатасет seed.py")
        examples = [
            ("Интеграции и IT", "[Демо] Не обновляются статусы между рабочими системами", "HIGH",
             "Демонстрационное обращение для подбора исполнителя. После обновления статуса в одной системе данные во второй остаются прежними. Нужно проверить обмен и предложить решение."),
            ("Оперативность", "[Демо] Заявки между отделами остаются без ответа", "MEDIUM",
             "Демонстрационное обращение для подбора исполнителя. Сотрудники не видят, кто принял межотдельскую заявку. Нужно определить порядок обработки и сроки ответа."),
        ]
        for category_name, title, priority, description in examples:
            existing = db.scalar(select(Ticket).where(Ticket.title == title))
            if existing:
                print(f"Уже существует: {existing.number}")
                continue
            category = db.scalar(select(Category).where(Category.name == category_name, Category.is_active.is_(True)))
            if not category:
                raise RuntimeError(f"Не найдена категория: {category_name}")
            if postgres:
                ticket_id = db.execute(text("SELECT nextval(pg_get_serial_sequence('tickets', 'id'))")).scalar_one()
            else:
                sequence = db.execute(text("SELECT seq FROM sqlite_sequence WHERE name='tickets'")).scalar() or 0
                ticket_id = sequence + 1
            ticket = Ticket(id=ticket_id, number=f"A-{ticket_id:03d}", type="PROBLEM", category_id=category.id,
                            title=title, description=description, priority=priority, status="NEW",
                            author_id=author.id, assignee_id=None,
                            due_at=now() + timedelta(hours=category.sla_first_response_hours))
            db.add(ticket)
            db.flush()
            db.add(TicketHistory(ticket_id=ticket.id, actor_id=admin.id, field="created", new_value="Демонстрационное обращение без ответственного"))
            print(f"Создано: {ticket.number} — {title}")
        db.commit()


if __name__ == "__main__":
    seed()
