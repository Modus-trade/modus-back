from test_workflow import clients  # noqa: F401

import app.services.ai as ai


def ticket(client, title, description, category=1, kind="PROBLEM"):
    r = client.post("/api/tickets", json={"type": kind, "category_id": category, "title": title, "description": description})
    assert r.status_code == 201, r.text
    return r.json()


def test_suggest_without_ai_uses_rules(clients, monkeypatch):
    employee = clients[0]
    monkeypatch.delenv("AI_URL", raising=False)
    r = employee.post("/api/assist/suggest", json={"title": "Курс по новинкам", "description": "Предлагаю провести обучение и вебинар по новым продуктам"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "rules"
    assert data["type"] == "PROPOSAL"
    assert data["category_id"] == 1 and data["assignee_name"] == "Пользователь 3" and data["sla_hours"] == 48
    thanks = employee.post("/api/assist/suggest", json={"title": "Спасибо", "description": "Большое спасибо коллегам за обучение"}).json()
    assert thanks["type"] == "PRAISE"
    assert employee.post("/api/assist/suggest", json={"title": "а", "description": "б"}).status_code == 422


def test_suggest_with_ai_and_bad_answer(clients, monkeypatch):
    employee = clients[0]
    monkeypatch.setattr(ai, "classify", lambda *a, **k: {"type": "PROBLEM", "category_id": 1, "confidence": 0.93, "reason": "Про обучение"})
    data = employee.post("/api/assist/suggest", json={"title": "Нет занятий", "description": "Не хватает практики с оборудованием"}).json()
    assert data["source"] == "ai" and data["category_id"] == 1 and data["confidence"] == 0.93 and data["reason"] == "Про обучение"
    # ИИ назвал несуществующую категорию — не верим, берём правила.
    monkeypatch.setattr(ai, "classify", lambda *a, **k: {"type": "PROBLEM", "category_id": 999, "confidence": 0.9, "reason": ""})
    assert employee.post("/api/assist/suggest", json={"title": "Нет занятий", "description": "Не хватает обучения на оборудовании"}).json()["source"] == "rules"


def test_ai_unreachable_falls_back_fast(clients, monkeypatch):
    monkeypatch.setenv("AI_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AI_TIMEOUT", "1")
    monkeypatch.setattr(ai, "_down_until", 0.0)
    r = clients[0].post("/api/assist/suggest", json={"title": "Обучение", "description": "Нужен курс по новым продуктам"})
    assert r.status_code == 200 and r.json()["source"] == "rules"
    assert ai._down_until > 0  # следующие запросы не ждут недоступный ИИ


def test_similar_and_solve(clients, monkeypatch):
    employee, other, manager, outside_manager, admin = clients
    monkeypatch.delenv("AI_URL", raising=False)
    base = ticket(employee, "Нет практики с оборудованием", "На обучении мало практики с новым оборудованием, нужны практические занятия")
    twin = ticket(employee, "Мало практики на обучении", "Хотим больше практических занятий с оборудованием на обучении")
    ticket(employee, "Сломался кондиционер", "В офисе жарко, кондиционер не работает уже неделю")

    assert employee.get(f"/api/tickets/{base['id']}/similar").status_code == 403
    items = manager.get(f"/api/tickets/{base['id']}/similar").json()["items"]
    assert [i["id"] for i in items] == [twin["id"]]
    assert {"number", "title", "status", "resolution", "score"} <= items[0].keys()
    assert outside_manager.get(f"/api/tickets/{base['id']}/similar").status_code == 404

    # Без ИИ и без решённых похожих — честно говорим, что подсказать нечего.
    assert manager.post(f"/api/tickets/{base['id']}/solve").status_code == 503
    assert employee.post(f"/api/tickets/{base['id']}/solve").status_code == 403

    seen = {}

    def fake_solve(t, similar):
        seen["similar"] = similar
        return {"summary": "Добавить практику", "steps": ["Выделить стенд", "Назначить занятия"], "based_on": [twin["number"]], "reply_draft": "Спасибо, займёмся."}

    monkeypatch.setattr(ai, "solve", fake_solve)
    data = manager.post(f"/api/tickets/{base['id']}/solve").json()
    assert data["source"] == "ai" and data["steps"] == ["Выделить стенд", "Назначить занятия"]
    assert seen["similar"][0]["number"] == twin["number"]
