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
    monkeypatch.setattr("app.services.deadlines.SessionLocal", sessions)
    monkeypatch.setattr("app.services.integrations.SessionLocal", sessions)
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
    assert manager.patch(path, json={"status": "CLOSED"}).status_code == 422
    implemented = employee.get(path).json()
    assert employee.post(path + "/resolution-review", json={
        "decision": "confirm", "expected_version": implemented["resolution_review_version"],
    }).status_code == 200
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
    assert {h["field"] for h in detail["history"]} == {"status", "priority", "resolution", "resolution_review"}
    assert detail["resolution_confirmed_at"]
    assert detail["resolution_reviews"][0]["decision"] == "confirm"
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


def test_cleared_filters_restore_results(clients):
    employee, _, _, _, admin = clients
    create(employee)
    for field in ("category_id", "assignee_id", "author_id", "overdue", "status", "type", "priority"):
        response = admin.get("/api/tickets", params={field: ""})
        assert response.status_code == 200, (field, response.text)
        assert response.json()["total"] == 1, field
    for field in ("category_id", "assignee_id", "overdue"):
        assert admin.get("/api/tickets", params={field: "invalid"}).status_code == 422
    assert admin.get("/api/tickets", params={"category_id": 2}).json()["total"] == 0
    assert admin.get("/api/tickets", params={"overdue": "false"}).json()["total"] == 1


def test_priority_sort_has_business_order(clients):
    employee, _, _, _, admin = clients
    for priority in ("MEDIUM", "HIGH", "LOW"):
        ticket = create(employee)
        assert admin.patch(f"/api/tickets/{ticket['id']}", json={"priority": priority}).status_code == 200
    for order, expected in (("asc", ["LOW", "MEDIUM", "HIGH"]), ("desc", ["HIGH", "MEDIUM", "LOW"])):
        rows = admin.get("/api/tickets", params={"sort": "priority", "order": order}).json()["items"]
        assert [row["priority"] for row in rows] == expected


@pytest.mark.parametrize("change_user", [{"is_active": False}, {"role": "EMPLOYEE"}])
def test_unavailable_assignee_releases_work_and_routing(clients, change_user):
    employee, _, _, _, admin = clients
    open_ticket = create(employee)
    closed_ticket = create(employee)
    path = f"/api/tickets/{closed_ticket['id']}"
    admin.patch(path, json={"status": "REJECTED", "resolution": "Нет ресурсов"})
    admin.patch(path, json={"status": "CLOSED"})
    assert admin.patch("/api/users/3", json=change_user).status_code == 200
    detail = admin.get(f"/api/tickets/{open_ticket['id']}").json()
    assert detail["assignee_id"] is None
    event = detail["history"][-1]
    assert (event["field"], event["old_value"], event["new_value"], event["actor_id"]) == ("assignee_id", "3", None, 5)
    assert admin.get(path).json()["assignee_id"] == 3
    assert admin.get("/api/tickets?unassigned=true").json()["total"] == 1
    assert next(c for c in admin.get("/api/categories").json() if c["id"] == 1)["default_assignee_id"] is None
    assert create(employee)["assignee_id"] is None
    assert admin.patch(f"/api/tickets/{open_ticket['id']}", json={"assignee_id": 4}).status_code == 200


def test_legacy_unavailable_assignment_is_visible_in_queue(clients):
    employee, _, _, _, admin = clients
    ticket = create(employee)
    with next(app.dependency_overrides[get_db]()) as db:
        db.get(User, 3).is_active = False
        db.commit()
    rows = admin.get("/api/tickets?unassigned=true").json()["items"]
    assert [r["id"] for r in rows] == [ticket["id"]]


def test_resolution_revisions_are_complete_and_atomic(clients):
    employee, _, manager, _, admin = clients
    ticket = create(employee)
    path = f"/api/tickets/{ticket['id']}"
    original = "Первый результат. " * 100
    replacement = "Уточнённый результат. " * 100
    assert manager.patch(path, json={"status": "REJECTED", "resolution": original}).status_code == 200
    assert manager.patch(path, json={"resolution": replacement}).status_code == 200
    assert manager.patch(path, json={"resolution": replacement}).status_code == 200
    assert manager.patch(path, json={"resolution": " "}).status_code == 422
    detail = employee.get(path).json()
    history = [h for h in detail["history"] if h["field"] == "resolution"]
    assert len(history) == 2
    assert (history[0]["old_value"], history[0]["new_value"]) == (None, original)
    assert (history[1]["old_value"], history[1]["new_value"]) == (original, replacement)
    assert history[1]["actor_id"] == 3
    assert detail["resolution"] == replacement
    assert admin.patch(path, json={"status": "CLOSED"}).status_code == 200
    assert admin.patch(path, json={"resolution": "Переписано"}).status_code == 422


def test_unicode_passwords_and_legacy_upgrade(clients):
    employee, _, _, _, admin = clients
    original = "я" * 40 + "a"
    other = "я" * 40 + "b"
    response = admin.post("/api/users", json={"email": "unicode@modus.demo", "password": original, "full_name": "Тест", "department": "Тест"})
    assert response.status_code == 201
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"email": "unicode@modus.demo", "password": other}).status_code == 401
        assert client.post("/api/auth/login", json={"email": "unicode@modus.demo", "password": original}).status_code == 200
        assert admin.patch(f"/api/users/{response.json()['id']}", json={"password": other}).status_code == 200
        assert client.post("/api/auth/login", json={"email": "unicode@modus.demo", "password": original}).status_code == 401
        assert client.post("/api/auth/login", json={"email": "unicode@modus.demo", "password": other}).status_code == 200
    with next(app.dependency_overrides[get_db]()) as db:
        db.get(User, 1).password_hash = passwords.handler("bcrypt").hash("demo1234")
        db.commit()
    assert employee.post("/api/auth/login", json={"email": "u1@modus.demo", "password": "demo1234"}).status_code == 200
    with next(app.dependency_overrides[get_db]()) as db:
        assert passwords.identify(db.get(User, 1).password_hash) == "bcrypt_sha256"
        db.get(User, 1).password_hash = passwords.handler("bcrypt").hash(original)
        db.commit()
    assert employee.post("/api/auth/login", json={"email": "u1@modus.demo", "password": other}).status_code == 401


def test_attention_overlaps_filters_pagination_and_activity(clients):
    from datetime import timedelta
    from app.models import Attachment, Comment, Ticket, TicketHistory, now
    employee, _, _, _, admin = clients
    ids = [create(employee, 2 if i in (0, 3) else 1)['id'] for i in range(8)]
    stamp = now()
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        for i, ticket_id in enumerate(ids):
            t = db.get(Ticket, ticket_id)
            t.created_at = stamp - timedelta(days=25)
            t.updated_at = stamp - timedelta(days=20 if i == 0 else 9) if i in (0, 2, 4, 5, 6, 7) else stamp
            t.due_at = stamp - timedelta(days=1) if i in (0, 1, 4) else stamp + timedelta(days=1)
            t.first_response_at = stamp - timedelta(days=15) if i in (2, 5, 6, 7) else None
            t.priority = 'HIGH' if i == 1 else 'LOW'
            if i == 4:
                t.status = 'IMPLEMENTED'
            if i == 5:
                db.add(Comment(ticket_id=ticket_id, author_id=1, body='Недавний ответ', created_at=stamp))
            if i == 6:
                db.add(TicketHistory(ticket_id=ticket_id, actor_id=3, field='priority', old_value='HIGH', new_value='LOW', created_at=stamp))
            if i == 7:
                db.add(Attachment(ticket_id=ticket_id, uploaded_by_id=1, filename='notes.txt', stored_path='/unused', content_type='text/plain', size_bytes=4, created_at=stamp))
        db.commit()
    finally:
        generator.close()
    response = admin.get('/api/analytics/attention')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['counts'] == {'total': 4, 'unassigned': 2, 'overdue': 2, 'resolution_overdue': 0, 'stale': 2}
    assert data['total'] == len({t['id'] for t in data['items']}) == 4
    assert data['items'][0]['id'] == ids[0]
    assert data['items'][0]['attention_reasons'] == ['unassigned', 'overdue', 'stale']
    assert data['notifications']['enabled'] is True
    assert admin.get('/api/analytics/attention?reason=stale&stale_days=14').json()['total'] == 1
    assert admin.get('/api/analytics/attention?reason=unassigned').json()['total'] == 2
    assert admin.get('/api/analytics/attention?priority=HIGH').json()['total'] == 1
    searched = admin.get('/api/analytics/attention', params={'q':data['items'][0]['number']}).json()
    assert searched['total'] == 1 and searched['counts']['total'] == 4
    first = admin.get('/api/analytics/attention?page_size=1').json()
    second = admin.get('/api/analytics/attention?page_size=1&page=2').json()
    assert first['total'] == second['total'] == 4
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert admin.get('/api/analytics/attention?stale_days=0').status_code == 422
    assert admin.get('/api/analytics/attention?reason=invalid').status_code == 422


def test_attention_access_empty_and_legacy_assignee(clients):
    from app.models import Ticket
    employee, _, manager, _, admin = clients
    assert employee.get('/api/analytics/attention').status_code == 403
    assert manager.get('/api/analytics/attention').status_code == 403
    assert admin.get('/api/analytics/attention').json()['counts']['total'] == 0
    item = create(employee)
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        db.get(User, 3).is_active = False
        db.commit()
    finally:
        generator.close()
    data = admin.get('/api/analytics/attention').json()
    assert data['counts']['unassigned'] == 1
    assert data['items'][0]['id'] == item['id']
    generator = app.dependency_overrides[get_db]()
    db = next(generator)
    try:
        db.get(Ticket, item['id']).status = 'CLOSED'
        db.commit()
    finally:
        generator.close()
    assert admin.get('/api/analytics/attention').json()['total'] == 0


def test_live_events_over_websocket(clients):
    employee, _, manager, _, admin = clients
    with manager.websocket_connect("/api/ws") as ws_manager, employee.websocket_connect("/api/ws") as ws_employee:
        ticket = employee.post(
            "/api/tickets",
            json={"type": "PROBLEM", "category_id": 1, "title": "Живое событие", "description": "Проверка уведомлений"},
        ).json()
        # Ответственный мгновенно получает уведомление и сигнал перечитать данные.
        events = [ws_manager.receive_json(), ws_manager.receive_json()]
        assert {e["type"] for e in events} == {"notification", "ticket_changed"}
        note = next(e for e in events if e["type"] == "notification")["notification"]
        assert note["ticket_id"] == ticket["id"] and note["kind"] == "assigned"
        assert note["href"] == f"/tickets/{ticket['id']}"
        assert ws_employee.receive_json()["type"] == "ticket_changed"

        manager.post(
            f"/api/tickets/{ticket['id']}/comments",
            json={"body": "Уточните, пожалуйста", "is_clarification_request": True},
        )
        events = [ws_employee.receive_json(), ws_employee.receive_json()]
        live = next(e for e in events if e["type"] == "notification")["notification"]
        assert live["kind"] == "clarification" and "body" not in live

    assert employee.get("/api/notifications").json()["unread_count"] == 1
    assert employee.post(f"/api/notifications/read-ticket/{ticket['id']}").status_code == 200
    assert employee.get("/api/notifications").json()["unread_count"] == 0


def test_websocket_requires_session(clients):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    import pytest
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/api/ws") as ws:
            ws.receive_text()


def test_websocket_with_mobile_token(clients):
    from fastapi.testclient import TestClient
    employee, _, manager, _, _ = clients
    token = manager.get("/api/ws-token").json()["token"]
    with TestClient(app).websocket_connect(f"/api/ws?token={token}") as ws:
        employee.post(
            "/api/tickets",
            json={"type": "PROBLEM", "category_id": 1, "title": "Мобильный сокет", "description": "Проверка токена"},
        )
        types = {ws.receive_json()["type"], ws.receive_json()["type"]}
        assert types == {"notification", "ticket_changed"}
    # Обычный JWT сессии не подходит как токен сокета.
    import pytest
    from starlette.websockets import WebSocketDisconnect
    session = manager.cookies.get("session")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(f"/api/ws?token={session}") as ws:
            ws.receive_text()
