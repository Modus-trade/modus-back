import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, get_db
from app.main import app
from app.models import Category, User
from app.security import passwords


@pytest.fixture()
def clients(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        for i, (role, department) in enumerate(
            [
                ("EMPLOYEE", "Закупки"),
                ("EMPLOYEE", "Продажи"),
                ("MANAGER", "Закупки"),
                ("MANAGER", "Продажи"),
                ("ADMIN", "Управление"),
            ],
            1,
        ):
            db.add(
                User(
                    id=i,
                    email=f"u{i}@modus.demo",
                    password_hash=passwords.hash("demo1234"),
                    full_name=f"Пользователь {i}",
                    role=role,
                    department=department,
                )
            )
        db.flush()
        db.add(
            Category(
                id=1,
                name="Обучение",
                default_assignee_id=3,
                sla_first_response_hours=48,
            )
        )
        db.add(Category(id=2, name="Без ответственного", sla_first_response_hours=1))
        db.commit()

    def override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr("app.routers.tickets.UPLOADS", tmp_path / "uploads")
    result = []
    for i in range(1, 6):
        client = TestClient(app)
        response = client.post(
            "/api/auth/login",
            json={"email": f"u{i}@modus.demo", "password": "demo1234"},
        )
        assert response.status_code == 200
        assert "httponly" in response.headers["set-cookie"].lower()
        result.append(client)
    yield result
    for client in result:
        client.close()
    app.dependency_overrides.clear()
    engine.dispose()


def create(client, category=1):
    r = client.post(
        "/api/tickets",
        json={
            "type": "PROPOSAL",
            "category_id": category,
            "title": "Обучение сотрудников",
            "description": "Нужно организовать занятия по оборудованию.",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_full_clarification_and_resolution_workflow(clients):
    employee, other, manager, outside, admin = clients
    ticket = create(employee)
    path = f"/api/tickets/{ticket['id']}"
    assert ticket["assignee_id"] == 3
    assert ticket["status"] == "NEW"
    assert other.get(path).status_code == 404
    assert outside.get(path).status_code == 404
    assert employee.patch(path, json={"status": "IN_REVIEW"}).status_code == 403
    assert manager.patch(path, json={"status": "CLOSED"}).status_code == 422
    assert manager.patch(path, json={"status": "REJECTED"}).status_code == 422
    assert (
        employee.post(
            path + "/comments",
            json={"body": "Запрос", "is_clarification_request": True},
        ).status_code
        == 403
    )
    assert (
        manager.post(
            path + "/comments",
            json={"body": "Уточните формат занятия", "is_clarification_request": True},
        ).status_code
        == 201
    )
    detail = employee.get(path).json()
    assert detail["status"] == "AWAITING_CLARIFICATION"
    assert detail["first_response_at"]
    assert (
        employee.post(
            path + "/comments", json={"body": "Практический семинар"}
        ).status_code
        == 201
    )
    assert manager.get(path).json()["status"] == "IN_REVIEW"
    assert manager.patch(path, json={"status": "IMPLEMENTED"}).status_code == 422
    assert manager.get(path).json()["status"] == "IN_REVIEW"
    assert (
        manager.patch(
            path,
            json={
                "status": "IMPLEMENTED",
                "resolution": "Семинар запланирован",
                "priority": "HIGH",
            },
        ).status_code
        == 200
    )
    assert manager.patch(path, json={"resolution": " "}).status_code == 422
    assert manager.patch(path, json={"status": "CLOSED"}).status_code == 200
    assert (
        employee.post(
            path + "/attachments", files={"file": ("late.txt", b"late", "text/plain")}
        ).status_code
        == 422
    )
    detail = employee.get(path).json()
    assert detail["resolution"] == "Семинар запланирован"
    assert detail["closed_at"]
    assert len(detail["comments"]) == 2
    assert {h["field"] for h in detail["history"]} == {"status", "priority"}
    assert manager.patch(path, json={"status": "NEW"}).status_code == 422
    assert (
        manager.post(
            path + "/comments", json={"body": "Поздний комментарий"}
        ).status_code
        == 422
    )


def test_scoped_list_filters_and_analytics(clients):
    employee, other, manager, outside, admin = clients
    create(employee)
    sales_ticket = create(other)
    assert employee.get("/api/tickets").json()["total"] == 1
    # The procurement manager is assigned both tickets, including the sales author.
    assert manager.get("/api/tickets").json()["total"] == 2
    assert outside.get("/api/tickets").json()["total"] == 1
    assert (
        outside.post(
            f"/api/tickets/{sales_ticket['id']}/comments",
            json={"body": "Принято руководителем подразделения"},
        ).status_code
        == 201
    )
    assert outside.get(f"/api/tickets/{sales_ticket['id']}").json()["first_response_at"]
    assert admin.get("/api/tickets", params={"q": "Обучение"}).json()["total"] == 2
    assert admin.get("/api/tickets", params={"type": "PROBLEM"}).json()["total"] == 0
    assert (
        admin.get("/api/tickets", params={"department": "Закупки"}).json()["total"] == 1
    )
    assert employee.get("/api/analytics/summary").status_code == 403
    assert admin.get("/api/analytics/summary").json()["total"] == 2
    assert employee.get("/api/users").status_code == 403
    assert manager.post("/api/categories", json={"name": "Новая"}).status_code == 403


def test_unassigned_and_attachment_access(clients):
    employee, other, manager, outside, admin = clients
    ticket = create(employee, 2)
    assert ticket["assignee_id"] is None
    assert admin.get("/api/tickets?unassigned=true").json()["total"] == 1
    assert employee.get("/api/tickets?unassigned=true").status_code == 403
    path = f"/api/tickets/{ticket['id']}"
    assert admin.patch(path, json={"assignee_id": 3}).status_code == 200
    history = admin.get(path).json()["history"]
    assert history[-1]["field"] == "assignee_id"
    assert admin.get("/api/tickets?unassigned=true").json()["total"] == 0
    r = employee.post(
        path + "/attachments", files={"file": ("notes.txt", b"demo", "text/plain")}
    )
    assert r.status_code == 201
    attachment = f"/api/attachments/{r.json()['id']}"
    assert employee.get(attachment).content == b"demo"
    assert other.get(attachment).status_code == 404
    assert (
        employee.post(
            path + "/attachments", files={"file": ("bad.exe", b"bad")}
        ).status_code
        == 422
    )
    assert (
        employee.post(
            path + "/attachments",
            files={"file": ("large.txt", b"a" * (10 * 1024 * 1024 + 1))},
        ).status_code
        == 413
    )


def test_auth_and_deactivation(clients):
    employee, other, manager, outside, admin = clients
    assert admin.patch("/api/users/1", json={"is_active": False}).status_code == 200
    assert employee.get("/api/auth/me").status_code == 401
    assert admin.patch("/api/users/5", json={"is_active": False}).status_code == 422
    assert (
        other.post(
            "/api/tickets", headers={"Origin": "https://evil.example"}, json={}
        ).status_code
        == 403
    )
    other.post("/api/auth/logout")
    assert other.get("/api/tickets").status_code == 401


def test_sla_first_reply_and_rejection(clients):
    from datetime import timedelta
    from app.models import Ticket, now

    employee, other, manager, outside, admin = clients
    ticket = create(employee)
    path = f"/api/tickets/{ticket['id']}"
    # Alter only the isolated test DB, to exercise the real overdue calculation.
    with next(app.dependency_overrides[get_db]()) as db:
        item = db.get(Ticket, ticket["id"])
        item.created_at = now() - timedelta(hours=60)
        item.due_at = now() - timedelta(hours=12)
        db.commit()
    assert admin.get("/api/tickets?overdue=true").json()["total"] == 1
    assert (
        employee.post(
            path + "/comments", json={"body": "Добавлю подробности"}
        ).status_code
        == 201
    )
    assert admin.get("/api/tickets?overdue=true").json()["total"] == 1
    assert (
        manager.post(path + "/comments", json={"body": "Приняли в работу"}).status_code
        == 201
    )
    assert admin.get("/api/tickets?overdue=true").json()["total"] == 0
    summary = admin.get("/api/analytics/summary").json()
    assert 59 < summary["average_first_response_hours"] < 61
    assert (
        manager.patch(
            path,
            json={
                "status": "REJECTED",
                "resolution": "Нет доступных ресурсов; вернёмся к вопросу в следующем квартале.",
            },
        ).status_code
        == 200
    )
    assert manager.patch(path, json={"status": "CLOSED"}).status_code == 200
    assert admin.get("/api/analytics/summary").json()["closed_percent"] == 100


def test_admin_validation_and_inactive_routing(clients):
    employee, other, manager, outside, admin = clients
    assert (
        admin.post(
            "/api/categories", json={"name": "Ошибочная", "default_assignee_id": 1}
        ).status_code
        == 422
    )
    assert (
        admin.post(
            "/api/categories", json={"name": "Ошибочная", "sla_first_response_hours": 0}
        ).status_code
        == 422
    )
    assert (
        admin.post(
            "/api/users",
            json={
                "email": "u1@modus.demo",
                "password": "demo1234",
                "full_name": "Дубль",
                "department": "Закупки",
            },
        ).status_code
        == 409
    )
    assert admin.patch("/api/users/3", json={"is_active": False}).status_code == 200
    ticket = create(employee)
    assert ticket["assignee_id"] is None
    assert admin.get("/api/tickets?unassigned=true").json()["total"] == 1
    assert (
        employee.post(
            "/api/tickets",
            json={
                "type": "PROBLEM",
                "category_id": 1,
                "title": "   ",
                "description": "           ",
            },
        ).status_code
        == 422
    )
    assert "password_hash" not in admin.get("/api/users").text
