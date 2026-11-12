from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_workflow import clients  # noqa: F401
from test_deadlines import database
from test_resolution_reviews import publish, review
from test_knowledge import article_data, approve_draft, prepare, view, feedback
from app.models import KnowledgeArticle, KnowledgeDraft, KnowledgeRevision, KnowledgeReport, now


def closed_ticket(clients):
    ticket = publish(clients[0], clients[2])
    assert review(clients[0], ticket).status_code == 200
    return ticket, f"/api/tickets/{ticket['id']}/knowledge-article"


def test_drafts_are_private_partial_and_company_publication_needs_admin(clients):
    author, colleague, manager, outside, admin = clients
    ticket = publish(author, manager)
    path = f"/api/tickets/{ticket['id']}/knowledge-article"
    assert manager.put(path, json=article_data()).status_code == 422
    assert review(author, ticket).status_code == 200
    assert author.put(path, json=article_data()).status_code == 403
    assert outside.get(path).status_code == 404
    assert manager.get(path).json() is None
    saved = manager.put(path, json=article_data(title='', summary='', body='')).json()
    assert saved['draft']['body'] == ''
    assert not saved['is_published']
    assert colleague.get(f"/api/knowledge/{saved['id']}").status_code == 404
    assert manager.post(path + '/submit', json={'expected_version': 1, 'reviewed': True}).status_code == 422
    assert manager.put(path, json=article_data(is_published=True)).status_code == 422
    saved = manager.put(path, json=article_data(expected_version=1)).json()
    assert colleague.get('/api/knowledge').json()['total'] == 0
    assert colleague.post('/api/knowledge/suggest', json={'text': saved['draft']['title']}).json()['items'] == []
    submitted = manager.post(path + '/submit', json={'expected_version': 2, 'reviewed': True})
    assert submitted.status_code == 200
    body = {'expected_version': 3, 'reviewed': True}
    assert manager.post(path + '/approve', json=body).status_code == 403
    assert admin.post(path + '/approve', json={**body, 'reviewed': False}).status_code == 422
    assert admin.post(path + '/approve', json=body).status_code == 200
    assert colleague.get('/api/knowledge').json()['total'] == 1
    assert author.get('/api/knowledge/manage').status_code == 403
    assert outside.get('/api/knowledge/manage').json()['items'] == []
    assert manager.get(path).json()['revisions'][0]['reviewer']


@pytest.mark.parametrize('field', ['title', 'summary', 'body'])
def test_secret_values_are_blocked_in_every_published_field(clients, field):
    _, path = closed_ticket(clients)
    saved = clients[2].put(path, json=article_data(**{field: 'password=SecretValue12345'})).json()
    assert saved['findings'][0]['severity'] == 'block'
    assert clients[2].post(path + '/submit', json={'expected_version': 1, 'reviewed': True, 'warnings_reviewed': True}).status_code == 422
    assert clients[4].post(path + '/approve', json={'expected_version': 1, 'reviewed': True, 'warnings_reviewed': True}).status_code == 409
    assert clients[1].get('/api/knowledge').json()['items'] == []


def test_personal_cases_and_contact_warnings_require_explicit_action(clients):
    _, path = closed_ticket(clients)
    manager, admin = clients[2], clients[4]
    manager.put(path, json=article_data(confidential_case=True))
    assert manager.post(path + '/submit', json={'expected_version': 1, 'reviewed': True}).status_code == 422
    saved = manager.put(path, json=article_data(expected_version=1, body='Обратитесь в общую службу поддержки support@example.test для проверки доступа.')).json()
    assert saved['findings'][0]['kind'] == 'email'
    assert manager.post(path + '/submit', json={'expected_version': 2, 'reviewed': True}).status_code == 422
    approve_draft(manager, admin, path, saved)
    assert clients[1].get('/api/knowledge').json()['total'] == 1


def test_department_is_bound_to_source_owner_and_rechecked_after_user_transfer(clients):
    author, colleague, manager, outside, admin = clients
    ticket, path = closed_ticket(clients)
    assert admin.patch(f"/api/tickets/{ticket['id']}", json={'assignee_id': 4}).status_code == 422  # closed ticket
    # Assignment is immaterial to the department audience, even when edited by an administrator.
    saved = admin.put(path, json=article_data(visibility='DEPARTMENT')).json()
    assert saved['draft']['owner_department'] == 'Закупки'
    approve_draft(admin, admin, path, saved)
    article = author.get('/api/knowledge').json()['items'][0]
    assert colleague.get(f"/api/knowledge/{article['id']}").status_code == 404
    assert outside.get(f"/api/knowledge/{article['id']}").status_code == 404
    assert admin.patch('/api/users/1', json={'department': 'Продажи'}).status_code == 200
    assert author.get('/api/knowledge').json()['items'] == []
    assert author.get(f"/api/knowledge/{article['id']}").status_code == 404
    assert view(author, article, str(uuid4())).status_code == 404


def test_department_manager_publishes_reviewed_draft_for_own_team(clients):
    author, other, manager, _, _ = clients
    _, path = closed_ticket(clients)
    saved = manager.put(path, json=article_data(visibility='DEPARTMENT')).json()
    approved = approve_draft(manager, manager, path, saved)
    assert approved['is_published']
    assert author.get('/api/knowledge').json()['total'] == 1
    assert other.get('/api/knowledge').json()['total'] == 0


def test_editing_published_text_requires_new_review_and_preserves_previous_version(clients):
    author, colleague, manager, _, admin = clients
    ticket, article = prepare(clients)
    path = f"/api/tickets/{ticket['id']}/knowledge-article"
    attempt = str(uuid4())
    assert view(author, article, attempt).status_code == 200
    version = manager.get(path).json()['draft']['version']
    saved = manager.put(path, json=article_data(expected_version=version, body='Обновлённая инструкция с новым порядком проверки синхронизации.')).json()
    assert colleague.get(f"/api/knowledge/{article['id']}").json()['body'] == article['body']
    assert feedback(author, article, attempt).status_code == 200
    assert manager.put(path, json=article_data(expected_version=version)).status_code == 409
    approved = approve_draft(manager, admin, path, saved)
    assert approved['published_version'] == 2
    assert manager.get(path + '/revisions/1').json()['body'] == article['body']
    assert colleague.get(path + '/revisions/1').status_code == 403
    assert feedback(author, article, attempt).status_code == 409
    assert manager.post(path + '/withdraw', json={'expected_version': 1}).status_code == 409
    assert manager.post(path + '/withdraw', json={'expected_version': 2}).status_code == 200
    assert manager.post(path + '/withdraw', json={'expected_version': 2}).status_code == 200
    assert colleague.get(f"/api/knowledge/{article['id']}").status_code == 404
    saved = manager.put(path, json=article_data(expected_version=approved['draft']['version'])).json()
    approve_draft(manager, admin, path, saved)
    with database() as db:
        db.get(KnowledgeArticle, article['id']).review_due_at = now() - timedelta(seconds=1)
        db.commit()
    assert colleague.get('/api/knowledge').json()['total'] == 0
    assert colleague.post('/api/knowledge/suggest', json={'text': article['title']}).json()['items'] == []
    assert colleague.get(f"/api/knowledge/{article['id']}").status_code == 404


def test_concurrent_approval_creates_one_revision(clients):
    _, path = closed_ticket(clients)
    manager, admin = clients[2], clients[4]
    manager.put(path, json=article_data())
    submitted = manager.post(path + '/submit', json={'expected_version': 1, 'reviewed': True}).json()
    data = {'expected_version': submitted['draft']['version'], 'reviewed': True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: admin.post(path + '/approve', json=data), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with database() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeRevision)) == 1
    draft = manager.get(path).json()['draft']
    manager.put(path, json=article_data(expected_version=draft['version']))
    assert admin.post(path + '/approve', json=data).status_code == 409


def test_reports_notify_admin_without_disclosing_report_to_other_readers(clients, monkeypatch):
    from app.realtime import hub
    author, colleague, manager, _, admin = clients
    ticket, article = prepare(clients)
    path = f"/api/tickets/{ticket['id']}/knowledge-article"
    events = []
    monkeypatch.setattr(hub, 'publish', lambda audience, message: events.append((audience, message)))
    report = {'expected_version': article['version'], 'reason': 'В примере указаны личные сведения о сотруднике.'}
    assert colleague.post(f"/api/knowledge/{article['id']}/report", json=report).status_code == 200
    assert colleague.post(f"/api/knowledge/{article['id']}/report", json=report).status_code == 200
    assert manager.get(path).json()['reports'] == []
    assert all(event['action'] != 'reported' for event in manager.get(path).json()['history'])
    assert admin.get(path).json()['reports'][0]['reason'] == report['reason']
    assert admin.get('/api/notifications').json()['items'][0]['kind'] == 'knowledge_report'
    assert 'личные сведения' not in str(author.get('/api/notifications').json())
    assert any(event == {'type': 'knowledge_changed'} for _, event in events)
    assert manager.post(path + '/withdraw', json={'expected_version': 1}).status_code == 200
    assert colleague.get(f"/api/knowledge/{article['id']}").status_code == 404
    with database() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeReport)) == 1
        assert not db.scalar(select(KnowledgeReport.is_open))


def test_removed_merge_routes_and_fields_are_absent(clients):
    from app.models import Ticket
    assert 'duplicate_of_id' not in Ticket.__table__.columns
    assert clients[4].post('/api/duplicates/merge', json={}).status_code == 404
    assert clients[4].get('/api/tickets/1/duplicates').status_code == 404
    assert 'duplicate_operations' not in Ticket.metadata.tables
    assert clients[4].post('/api/problems/similar', json={'text': 'Рабочая система'}).status_code == 404
    assert clients[4].get('/api/tickets/1/similar').status_code == 404


def test_upgrade_withdraws_legacy_text_into_private_draft_and_preserves_source(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text, inspect, event
    from app.database import BASE_DIR

    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge-review.db'}")

    @event.listens_for(engine, 'connect')
    def foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')

    monkeypatch.setattr('app.database.engine', engine)
    config = Config(str(BASE_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(BASE_DIR / 'alembic'))
    try:
        command.upgrade(config, 'head')
        command.downgrade(config, '008')
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO users (id,email,password_hash,full_name,role,department,position,is_active,created_at) VALUES (1,'legacy@example.test','hash','Legacy','ADMIN','Office','',1,'2026-01-01')"))
            connection.execute(text("INSERT INTO categories (id,name,description,sla_first_response_hours,is_active,sort_order) VALUES (1,'Legacy','',48,1,0)"))
            connection.execute(text("INSERT INTO tickets (id,number,type,category_id,title,description,author_id,is_shared,priority,status,created_at,updated_at,due_at) VALUES (1,'A-001','PROBLEM',1,'Private source','Private description',1,0,'HIGH','CLOSED','2026-01-01','2026-01-01','2026-01-03')"))
            connection.execute(text("INSERT INTO knowledge_articles (id,source_ticket_id,category_id,title,summary,body,visibility,department,is_published,version,editor_id,updated_at,review_due_at) VALUES (1,1,1,'Legacy title','Legacy summary','Original text','COMPANY',NULL,1,7,1,'2026-01-01','2027-01-01')"))
        command.upgrade(config, 'head')
        with engine.connect() as connection:
            assert connection.execute(text('SELECT body,is_published,version FROM knowledge_articles')).one() == ('Original text', 0, 7)
            assert connection.execute(text('SELECT title,body,status,owner_department FROM knowledge_drafts')).one() == ('Legacy title', 'Original text', 'DRAFT', 'Office')
            assert connection.scalar(text('SELECT description FROM tickets')) == 'Private description'
            assert connection.scalar(text('SELECT count(*) FROM knowledge_revisions')) == 0
            assert not connection.execute(text('PRAGMA foreign_key_check')).all()
        command.downgrade(config, '008')
        assert 'knowledge_drafts' not in inspect(engine).get_table_names()
        command.upgrade(config, 'head')
    finally:
        engine.dispose()
