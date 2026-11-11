"""Idempotent demo seed. Never overwrites existing records."""

import sys
sys.stdout.reconfigure(encoding="utf-8")  # Windows: русский вывод не падает в консоли cp1252/cp866
import json
from datetime import timedelta
from sqlalchemy import select
from app.database import SessionLocal, BASE_DIR
from app.models import Category, Comment, Ticket, TicketHistory, User, now
from app.security import passwords

TITLES = [
    "Обязательные поля в заявке на оборудование",
    "Статусы проектов для отдела закупок",
    "Единые обозначения изделий в каталоге",
    "Календарь обучения по новым продуктам",
    "Прозрачный маршрут согласования документов",
    "Пошаговые памятки для общих процессов",
    "Синхронизация данных рабочих систем",
    "Памятка для новых сотрудников",
    "Ответственные за версии приложений к договору",
    "Подтверждение результата перед закрытием задачи",
    "Сроки ответов смежных подразделений",
    "Единое место для итоговых решений",
    "История изменений спецификаций",
    "Планирование загрузки проектной группы",
    "Единый шаблон постановки задач",
    "Практические занятия с оборудованием",
    "Зона для коротких перерывов",
    "Спасибо за внутренние учебные встречи",
    "Правила хранения актуальных инструкций",
    "Обзор реализованных улучшений",
]


def seed():
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)):
            print("Данные уже существуют. Seed пропущен.")
            return
        password_hash = passwords.hash("demo1234")

        def user(email, name, role, department, position=""):
            item = User(
                email=email + "@modus.demo",
                full_name=name,
                role=role,
                department=department,
                position=position,
                password_hash=password_hash,
            )
            db.add(item)
            db.flush()
            return item

        user(
            "admin", "Александр Модусов", "ADMIN", "Управление", "Администратор системы"
        )
        departments = [
            "Закупки",
            "Продажи",
            "Бухгалтерия",
            "Проектирование",
            "IT",
            "Офис",
        ]
        emails = ["procurement", "sales", "accounting", "design", "it", "office"]
        names = [
            "Анна Смирнова",
            "Дмитрий Волков",
            "Елена Петрова",
            "Михаил Орлов",
            "Илья Соколов",
            "Мария Козлова",
        ]
        managers = [
            user("manager." + e, n, "MANAGER", d, "Руководитель подразделения")
            for e, n, d in zip(emails, names, departments)
        ]
        authors = [
            user(
                "employee" if i == 0 else "employee." + e,
                n,
                "EMPLOYEE",
                d,
                "Специалист",
            )
            for i, (e, n, d) in enumerate(
                zip(
                    emails,
                    [
                        "Алексей Иванов",
                        "Ольга Морозова",
                        "Наталья Белова",
                        "Сергей Ким",
                        "Артём Ли",
                        "Виктория Павлова",
                    ],
                    departments,
                )
            )
        ]
        records = json.loads((BASE_DIR / "dataset.json").read_text(encoding="utf-8"))
        categories = {}
        mapping = [0, 0, 3, 5, 2, 5, 4, 5, 1, 1, 1, 4, 3, 3, 3, 5, 5, 5, 5, 0]
        types = {
            "Проблема": "PROBLEM",
            "Предложение": "PROPOSAL",
            "Положительный отзыв": "PRAISE",
        }
        priorities = {"Низкий": "LOW", "Средний": "MEDIUM", "Высокий": "HIGH"}
        statuses = {
            "Новое": "NEW",
            "На рассмотрении": "IN_REVIEW",
            "В работе": "IN_PROGRESS",
            "Ожидает уточнения": "AWAITING_CLARIFICATION",
            "Реализовано": "IMPLEMENTED",
            "Отклонено": "REJECTED",
            "Закрыто": "CLOSED",
        }
        for i, row in enumerate(records):
            if row["category"] not in categories:
                category = Category(
                    name=row["category"],
                    description="Обращения по теме «" + row["category"] + "»",
                    default_assignee_id=managers[mapping[i]].id,
                    sort_order=len(categories),
                )
                db.add(category)
                db.flush()
                categories[row["category"]] = category
            category = categories[row["category"]]
            author_index = (
                0
                if "закупок" in row["author"]
                else 2
                if "бухгалтерии" in row["author"]
                else 1
                if "продажам" in row["author"]
                else 5
                if "офиса" in row["author"]
                else 3
            )
            created = now() - timedelta(days=(i * 7 % 58) + 1, hours=i % 12)
            status = statuses[row["status"]]
            first = None if status == "NEW" else created + timedelta(hours=2 + i % 9)
            # Two NEW records breach SLA; the other three are recent.
            if row["number"] in ("A-016", "A-020"):
                created = now() - timedelta(hours=4 + i % 8)
            resolution = (
                row["action"]
                .removeprefix("Пример результата: ")
                .removeprefix("Пример мотивированного ответа: ")
                if status in ("IMPLEMENTED", "REJECTED", "CLOSED")
                else None
            )
            ticket = Ticket(
                number=row["number"],
                type=types[row["type"]],
                category_id=category.id,
                title=TITLES[i],
                description=row["description"],
                author_id=authors[author_index].id,
                assignee_id=category.default_assignee_id,
                priority=priorities[row["priority"]],
                status=status,
                resolution=resolution,
                created_at=created,
                updated_at=first or created,
                first_response_at=first,
                due_at=created + timedelta(hours=48),
                closed_at=first + timedelta(days=1) if status == "CLOSED" else None,
            )
            db.add(ticket)
            db.flush()
            if status != "NEW":
                db.add(
                    TicketHistory(
                        ticket_id=ticket.id,
                        actor_id=ticket.assignee_id,
                        field="status",
                        old_value="NEW",
                        new_value=status,
                        created_at=first,
                    )
                )
                db.add(
                    Comment(
                        ticket_id=ticket.id,
                        author_id=ticket.assignee_id,
                        body=row["action"],
                        is_clarification_request=status == "AWAITING_CLARIFICATION",
                        created_at=first,
                    )
                )
        db.commit()
        print("Создано 13 пользователей, 18 категорий и 20 обращений.")


if __name__ == "__main__":
    seed()
