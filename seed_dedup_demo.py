"""Optional idempotent demo: one shared problem affecting five demo employees."""
import sys
sys.stdout.reconfigure(encoding="utf-8")  # Windows: русский вывод не падает в консоли cp1252/cp866
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.database import SessionLocal
from app.models import Category, Ticket, TicketParticipant, User, now
from app.routers.tickets import next_ticket_id


def seed():
    with SessionLocal() as db:
        employees = db.scalars(select(User).where(User.email.like("%@modus.demo"), User.role == "EMPLOYEE", User.is_active.is_(True)).order_by(User.id)).all()
        category = db.scalar(select(Category).where(Category.name == "Интеграции и IT", Category.is_active.is_(True)))
        if len(employees) < 5 or not category:
            raise RuntimeError("Для примера необходим основной демодатасет seed.py")
        title = "[Демо] Ошибка синхронизации статусов рабочих систем"
        ticket = db.scalar(select(Ticket).where(Ticket.title == title))
        if ticket is None:
            ticket_id = next_ticket_id(db)
            ticket = Ticket(id=ticket_id, number=f"A-{ticket_id:03d}", title=title,
                description="Синтетический пример: сотрудники видят разные статусы одной заявки в рабочих системах. Нужно восстановить обмен данными.",
                type="PROBLEM", category_id=category.id, author_id=employees[0].id,
                assignee_id=category.default_assignee_id, is_shared=True,
                status="NEW", priority="HIGH", due_at=now() + timedelta(hours=48))
            db.add(ticket)
            db.flush()
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        for employee in employees[1:5]:
            db.execute(insert(TicketParticipant).values(ticket_id=ticket.id, user_id=employee.id, created_at=now()).on_conflict_do_nothing(index_elements=["ticket_id", "user_id"]))
        db.commit()
        print(f"{ticket.number}: общая демонстрационная проблема, 5 сотрудников")


if __name__ == "__main__":
    seed()
