from test_workflow import clients, create  # noqa: F401


def problem(client, title="Синхронизация данных рабочих систем", shared=True):
    r = client.post("/api/tickets", json={"type": "PROBLEM", "category_id": 1,
        "title": title, "description": "Закрытые данные проекта и личные сведения", "is_shared": shared})
    assert r.status_code == 201, r.text
    return r.json()


def test_join_unique_private_and_sorted(clients):
    author, other, manager, outside, admin = clients
    ticket = problem(author)
    problem(author, "Сломался кофейный аппарат")
    items = other.post("/api/problems/similar", json={"text": "Проблемы синхронизации данных рабочих систем"}).json()["items"]
    assert len(items) == 1 and items[0]["id"] == ticket["id"]
    assert items[0]["affected_count"] == 1
    assert not {"description", "author_name", "author_id", "resolution", "comments", "attachments"} & items[0].keys()
    for _ in range(2):
        r = other.post(f"/api/problems/{ticket['id']}/join")
        assert r.status_code == 200 and r.json()["affected_count"] == 2
    assert author.post(f"/api/problems/{ticket['id']}/join").json()["affected_count"] == 2
    # Участник видит карточку только для чтения и без приватных полей автора.
    shared_view = other.get(f"/api/tickets/{ticket['id']}").json()
    assert shared_view["viewer"] == "participant"
    assert shared_view["description"] == "" and shared_view["author_name"] == "" and shared_view["comments"] == []
    assert other.post(f"/api/tickets/{ticket['id']}/comments", json={"body": "test"}).status_code == 404
    joined = other.get("/api/problems/joined").json()
    assert joined["total"] == 1 and joined["items"][0]["status"] == "NEW"
    assert author.get("/api/problems/joined").json()["total"] == 0
    ranked = manager.get("/api/tickets?sort=affected_count&order=desc").json()["items"]
    assert ranked[0]["id"] == ticket["id"] and ranked[0]["affected_count"] == 2
    assert admin.get("/api/tickets").json()["total"] == 2
    assert other.get("/api/problems/joined?page=0").status_code == 422


def test_privacy_and_lifecycle(clients):
    author, other, manager, outside, admin = clients
    ticket = problem(author, shared=False)
    assert other.post("/api/problems/similar", json={"text": ticket["title"]}).json()["items"] == []
    assert other.post(f"/api/problems/{ticket['id']}/join").status_code == 404
    assert admin.patch(f"/api/tickets/{ticket['id']}", json={"is_shared": True}).status_code == 200
    assert admin.patch(f"/api/tickets/{ticket['id']}", json={"is_shared": False}).status_code == 422
    assert other.post(f"/api/problems/{ticket['id']}/join").status_code == 200
    assert admin.patch(f"/api/tickets/{ticket['id']}", json={"status": "REJECTED", "resolution": "Решение принято"}).status_code == 200
    assert outside.post(f"/api/problems/{ticket['id']}/join").status_code == 409
    assert other.post(f"/api/problems/{ticket['id']}/join").status_code == 200
    assert other.get("/api/problems/joined").json()["items"][0]["status"] == "REJECTED"
    assert other.post("/api/problems/similar", json={"text": ticket["title"]}).json()["items"] == []
    proposal = create(author)
    assert admin.patch(f"/api/tickets/{proposal['id']}", json={"is_shared": True}).status_code == 422
    assert other.post(f"/api/problems/{proposal['id']}/join").status_code == 404
    assert author.patch(f"/api/tickets/{ticket['id']}", json={"is_shared": True}).status_code == 403


def test_similarity_validation(clients):
    author, other, manager, outside, admin = clients
    problem(author)
    assert other.post("/api/problems/similar", json={"text": "Оформление заявок на отпуск"}).json()["items"] == []
    assert other.post("/api/problems/similar", json={"text": "aa"}).status_code == 422
    assert other.post("/api/problems/similar", json={"text": "x" * 20501}).status_code == 422
    assert other.post("/api/tickets", json={"type": "PRAISE", "category_id": 1, "title": "Спасибо коллегам", "description": "Отличная совместная работа", "is_shared": True}).status_code == 422


def test_repeated_problems_in_summary(clients):
    author, other, manager, outside, admin = clients
    ticket = problem(author)
    problem(author, "Сломался кофейный аппарат")
    assert admin.get("/api/analytics/summary").json()["repeated"] == []
    assert other.post(f"/api/problems/{ticket['id']}/join").status_code == 200
    repeated = admin.get("/api/analytics/summary").json()["repeated"]
    assert [(r["id"], r["affected_count"]) for r in repeated] == [(ticket["id"], 2)]
