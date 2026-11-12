from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from test_workflow import clients, create  # noqa: F401
from test_deadlines import database
from test_resolution_reviews import publish, review
from app.models import KnowledgeArticle, KnowledgeAttempt, KnowledgeExposure, Ticket, now


def article_data(**changes):
    return {
        "title": "Настройка синхронизации рабочих систем",
        "summary": "Если статусы документов не обновляются после синхронизации рабочих систем.",
        "body": "Проверьте подключение к сети. Запустите синхронизацию рабочих систем и проверьте статус документов.",
        "visibility": "COMPANY", "expected_version": 0,
        "review_days": 90, **changes,
    }


def approve_draft(editor, approver, path, saved):
    reviewed = {'expected_version': saved['draft']['version'], 'reviewed': True, 'warnings_reviewed': True}
    submitted = editor.post(path + '/submit', json=reviewed)
    assert submitted.status_code == 200, submitted.text
    reviewed['expected_version'] = submitted.json()['draft']['version']
    result = approver.post(path + '/approve', json=reviewed)
    assert result.status_code == 200, result.text
    return result.json()


def prepare(clients, **changes):
    author, _, manager, _, admin = clients
    ticket = publish(author, manager)
    response = review(author, ticket)
    assert response.status_code == 200, response.text
    response = manager.put(f"/api/tickets/{ticket['id']}/knowledge-article", json=article_data(**changes))
    assert response.status_code == 200, response.text
    path = f"/api/tickets/{ticket['id']}/knowledge-article"
    article = approve_draft(manager, admin, path, response.json())
    public = author.get(f"/api/knowledge/{article['id']}")
    assert public.status_code == 200, public.text
    return ticket, public.json()


def view(client, article, attempt_id):
    return client.post(f"/api/knowledge/{article['id']}/view", json={
        "attempt_id": attempt_id, "expected_version": article['version'],
    })


def feedback(client, article, attempt_id, helped=True):
    return client.post(f"/api/knowledge/{article['id']}/feedback", json={
        "attempt_id": attempt_id, "expected_version": article['version'], "helped": helped,
    })


def test_only_reviewed_publication_is_searchable_without_exposing_source(clients):
    author, colleague, manager, _, _ = clients
    ticket, article = prepare(clients)
    with database() as db:
        source = db.get(Ticket, ticket['id'])
        source.description = 'секретконтрагента закрытый текст переписки'
        db.commit()
    result = colleague.post('/api/knowledge/suggest', json={'text': 'Синхронизация рабочих систем не обновляет документы'})
    assert result.status_code == 200
    assert result.json()['items'][0]['id'] == article['id']
    assert 'no-store' in result.headers['cache-control']
    assert 'body' not in result.json()['items'][0]
    assert colleague.get(f"/api/tickets/{ticket['id']}").status_code == 404
    result = colleague.get(f"/api/knowledge/{article['id']}")
    assert result.status_code == 200
    for key in ('source_ticket_id', 'author_id', 'editor_id', 'resolution', 'description', 'attachments', 'comments'):
        assert key not in result.json()
    assert 'секретконтрагента' not in result.text
    assert colleague.post('/api/knowledge/suggest', json={'text': 'секретконтрагента закрытый текст'}).json()['items'] == []
    assert colleague.post('/api/knowledge/suggest', json={'text': 'синхронизация'}).json()['items'] == []
    assert author.get('/api/knowledge/metrics').status_code == 403
    assert manager.get(f"/api/tickets/{ticket['id']}/knowledge-article").json()['draft']['body'] == article['body']


def test_initial_list_shows_instructions_and_clearing_search_restores_them(clients):
    _, employee, _, _, _ = clients
    _, article = prepare(clients)
    initial = employee.get('/api/knowledge')
    assert initial.status_code == 200
    assert initial.json()['total'] == 1
    assert initial.json()['items'][0]['id'] == article['id']
    assert employee.get('/api/knowledge', params={'q': 'синхронизации'}).json()['total'] == 1
    assert employee.get('/api/knowledge', params={'q': 'несуществующаяинструкция'}).json()['total'] == 0
    cleared = employee.get('/api/knowledge', params={'q': ''})
    assert cleared.json() == initial.json()
    assert employee.get('/api/knowledge', params={'q': '   '}).json() == initial.json()
    searched = employee.post('/api/knowledge/search', json={'q': 'синхронизации'})
    assert searched.status_code == 200
    assert searched.json() == initial.json()


def test_department_visibility_applies_to_list_suggestions_detail_view_and_feedback(clients):
    author, colleague, manager, other_manager, admin = clients
    ticket, article = prepare(clients, visibility='DEPARTMENT')
    attempt_id = str(uuid4())
    assert view(author, article, attempt_id).status_code == 200
    assert feedback(author, article, attempt_id).status_code == 200
    for client in (colleague, other_manager):
        assert client.get('/api/knowledge').json()['total'] == 0
        assert client.post('/api/knowledge/search', json={'q': article['title']}).json()['total'] == 0
        assert client.post('/api/knowledge/suggest', json={'text': article['title']}).json()['items'] == []
        assert client.get(f"/api/knowledge/{article['id']}").status_code == 404
        assert view(client, article, str(uuid4())).status_code == 404
        assert feedback(client, article, attempt_id).status_code == 404
    assert manager.get('/api/knowledge/metrics').json()['resolved_attempts'] == 1
    assert other_manager.get('/api/knowledge/metrics').json()['viewed_attempts'] == 0
    assert admin.get('/api/knowledge/metrics').json()['resolved_attempts'] == 1


def test_metrics_count_explicit_outcomes_once_and_allow_changed_mind(clients):
    author, colleague, _, _, admin = clients
    _, article = prepare(clients)
    attempt_id = str(uuid4())
    assert feedback(author, article, attempt_id).status_code == 404
    assert admin.get('/api/knowledge/metrics').json()['viewed_attempts'] == 0
    assert view(author, article, attempt_id).status_code == 200
    assert view(author, article, attempt_id).status_code == 200
    assert view(colleague, article, attempt_id).status_code == 404
    assert feedback(colleague, article, attempt_id).status_code == 404
    first = admin.get('/api/knowledge/metrics').json()
    assert (first['viewed_attempts'], first['resolved_attempts'], first['pending_attempts']) == (1, 0, 1)
    assert feedback(author, article, attempt_id).status_code == 200
    assert feedback(author, article, attempt_id).status_code == 200
    assert admin.get('/api/knowledge/metrics').json()['resolved_attempts'] == 1
    assert feedback(author, article, attempt_id, False).status_code == 200
    assert admin.get('/api/knowledge/metrics').json()['resolved_attempts'] == 0
    assert view(author, article, attempt_id).json()['helped'] is False
    with database() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeExposure)) == 1
        db.get(KnowledgeAttempt, attempt_id).created_at = now() - timedelta(days=31)
        db.commit()
    assert admin.get('/api/knowledge/metrics').json()['viewed_attempts'] == 0


def test_creating_ticket_replaces_self_reported_resolution_and_is_idempotent(clients):
    author, colleague, _, _, admin = clients
    _, article = prepare(clients)
    attempt_id = str(uuid4())
    assert view(author, article, attempt_id).status_code == 200
    assert feedback(author, article, attempt_id).status_code == 200
    payload = {'type': 'PROBLEM', 'category_id': 1, 'title': 'Синхронизация всё ещё сломана',
               'description': 'Испробовал инструкцию, но проблема повторилась.', 'self_service_attempt_id': attempt_id}
    assert colleague.post('/api/tickets', json=payload).status_code == 404
    first = author.post('/api/tickets', json=payload)
    retry = author.post('/api/tickets', json=payload)
    assert first.status_code == retry.status_code == 201
    assert first.json()['id'] == retry.json()['id']
    counts = admin.get('/api/knowledge/metrics').json()
    assert (counts['resolved_attempts'], counts['submitted_attempts'], counts['viewed_attempts']) == (0, 1, 1)
    assert feedback(author, article, attempt_id).status_code == 409
    assert view(author, article, attempt_id).status_code == 409


def test_joining_problem_replaces_resolution_without_creating_another_ticket(clients):
    author, colleague, _, _, admin = clients
    _, article = prepare(clients)
    problem = colleague.post('/api/tickets', json={'type': 'PROBLEM', 'is_shared': True, 'category_id': 1,
        'title': 'Синхронизация рабочих систем', 'description': 'Статусы не обновляются после синхронизации.'}).json()
    attempt_id = str(uuid4())
    assert view(author, article, attempt_id).status_code == 200
    assert feedback(author, article, attempt_id).status_code == 200
    path = f"/api/problems/{problem['id']}/join"
    assert author.post(path, json={'self_service_attempt_id': attempt_id}).status_code == 200
    assert author.post(path, json={'self_service_attempt_id': attempt_id}).status_code == 200
    counts = admin.get('/api/knowledge/metrics').json()
    assert (counts['resolved_attempts'], counts['joined_attempts'], counts['submitted_attempts']) == (0, 1, 0)
    assert feedback(author, article, attempt_id).status_code == 409


def test_concurrent_feedback_and_creation_keep_metrics_consistent(clients):
    author, _, _, _, admin = clients
    _, article = prepare(clients)
    attempt_id = str(uuid4())
    assert view(author, article, attempt_id).status_code == 200
    payload = {'type': 'PROBLEM', 'category_id': 1, 'title': 'Синхронизация не работает',
               'description': 'Нужна помощь с настройкой синхронизации.', 'self_service_attempt_id': attempt_id}
    with ThreadPoolExecutor(max_workers=2) as workers:
        future_feedback = workers.submit(feedback, author, article, attempt_id)
        future_ticket = workers.submit(author.post, '/api/tickets', json=payload)
        assert future_feedback.result().status_code in (200, 409)
        assert future_ticket.result().status_code == 201
    counts = admin.get('/api/knowledge/metrics').json()
    assert (counts['resolved_attempts'], counts['submitted_attempts']) == (0, 1)


def test_migration_keeps_existing_tickets_and_starts_with_no_publications(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, event, inspect, text
    from app.database import BASE_DIR
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge-migration.db'}")

    @event.listens_for(engine, 'connect')
    def enforce_foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')

    monkeypatch.setattr('app.database.engine', engine)
    config = Config(str(BASE_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(BASE_DIR / 'alembic'))
    try:
        command.upgrade(config, '007')
        # Initial migration uses current metadata: remove future tables to
        # reproduce the schema already deployed before knowledge was added.
        with engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                if table.name.startswith('knowledge_'):
                    table.drop(connection)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO users (id,email,password_hash,full_name,role,department,position,is_active,created_at) VALUES (1,'legacy@demo.test','hash','Legacy','ADMIN','Office','',1,'2026-01-01')"))
            connection.execute(text("INSERT INTO categories (id,name,description,sla_first_response_hours,is_active,sort_order) VALUES (1,'Legacy','',48,1,0)"))
            connection.execute(text("INSERT INTO tickets (id,number,type,category_id,title,description,author_id,is_shared,priority,status,created_at,updated_at,due_at) VALUES (1,'A-001','PROBLEM',1,'Legacy','Private text',1,0,'HIGH','CLOSED','2026-01-01','2026-01-01','2026-01-03')"))
        command.upgrade(config, 'head')
        with engine.connect() as connection:
            assert connection.scalar(text('SELECT description FROM tickets')) == 'Private text'
            assert connection.scalar(text('SELECT count(*) FROM knowledge_articles')) == 0
            assert not connection.execute(text('PRAGMA foreign_key_check')).all()
        assert 'audio_id' in {column['name'] for column in inspect(engine).get_columns('comments')}
        command.downgrade(config, '007')
        assert 'knowledge_articles' not in inspect(engine).get_table_names()
        command.upgrade(config, 'head')
    finally:
        engine.dispose()
