from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_workflow import clients, create  # noqa: F401
from test_deadlines import database, dispatch, set_deadline
from app.main import app
from app.models import Notification, ResolutionReview, Ticket, now


def publish(author, manager, ticket=None, result="Работа выполнена"):
    ticket = ticket or create(author)
    path = f"/api/tickets/{ticket['id']}"
    current = manager.get(path).json()
    if current['status'] == 'NEW':
        assert manager.patch(path, json={'status': 'IN_PROGRESS'}).status_code == 200
    response = manager.patch(path, json={'status': 'IMPLEMENTED', 'resolution': result})
    assert response.status_code == 200, response.text
    return response.json()


def review(client, ticket, decision='confirm', explanation='', version=None):
    return client.post(f"/api/tickets/{ticket['id']}/resolution-review", json={
        'decision': decision, 'explanation': explanation,
        'expected_version': ticket['resolution_review_version'] if version is None else version,
    })


def test_author_confirmation_closes_once_and_notifies_executor(clients):
    author, _, manager, _, admin = clients
    ticket = publish(author, manager)
    assert ticket['resolution_review_version'] == 1
    assert ticket['resolution_confirmed_at'] is None
    path = f"/api/tickets/{ticket['id']}"
    assert manager.patch(path, json={'status': 'CLOSED'}).status_code == 422
    assert admin.patch(path, json={'status': 'CLOSED'}).status_code == 422
    confirmed = review(author, ticket)
    assert confirmed.status_code == 200, confirmed.text
    closed = confirmed.json()
    assert closed['status'] == 'CLOSED'
    assert closed['closed_at'] == closed['resolution_confirmed_at']
    assert closed['first_response_at'] == ticket['first_response_at']
    assert manager.get('/api/notifications').json()['items'][0]['kind'] == 'resolution_confirmed'
    before = admin.get('/api/notifications').json()['total']
    repeat = review(author, ticket)
    assert repeat.status_code == 200
    assert repeat.json()['closed_at'] == closed['closed_at']
    assert admin.get('/api/notifications').json()['total'] == before
    detail = author.get(path).json()
    assert len(detail['resolution_reviews']) == 1
    saved = detail['resolution_reviews'][0]
    assert (saved['version'], saved['decision'], saved['author_id']) == (1, 'confirm', 1)
    assert saved['resolution_snapshot'] == ticket['resolution']
    assert len([item for item in detail['history'] if item['field'] == 'resolution_review']) == 1
    assert review(author, ticket, 'rework', 'Другое решение').status_code == 409


def test_rework_requires_explanation_and_keeps_resolution_and_deadline(clients):
    author, _, manager, _, admin = clients
    ticket = create(author)
    deadline = (now() + timedelta(hours=3)).isoformat() + 'Z'
    assert set_deadline(manager, ticket, deadline).status_code == 200
    ticket = publish(author, manager, ticket)
    assert review(author, ticket, 'rework', '   ').status_code == 422
    path = f"/api/tickets/{ticket['id']}"
    assert author.get(path).json()['status'] == 'IMPLEMENTED'
    explanation = '  Проблема сохраняется. ' + 'Подробности. ' * 100 + '  '
    response = review(author, ticket, 'rework', explanation)
    assert response.status_code == 200, response.text
    returned = response.json()
    assert returned['status'] == 'IN_PROGRESS'
    assert returned['closed_at'] is None and returned['resolution_confirmed_at'] is None
    assert returned['resolution'] == ticket['resolution']
    assert returned['resolution_due_at'] == ticket['resolution_due_at']
    assert returned['resolution_due_version'] == ticket['resolution_due_version'] + 1
    assert returned['due_at'] == ticket['due_at']
    assert returned['first_response_at'] == ticket['first_response_at']
    assert manager.get('/api/notifications').json()['items'][0]['kind'] == 'resolution_rework'
    detail = author.get(path).json()
    assert detail['resolution_reviews'][0]['explanation'] == explanation.strip()
    event = next(item for item in detail['history'] if item['field'] == 'resolution_review')
    assert event['new_value'] == 'rework' and event['reason'] == explanation.strip()
    before = admin.get('/api/notifications').json()['total']
    assert review(author, ticket, 'rework', explanation).status_code == 200
    assert admin.get('/api/notifications').json()['total'] == before
    assert author.get(path).json()['resolution_due_version'] == returned['resolution_due_version']
    assert review(author, ticket, 'rework', 'Переписанное пояснение').status_code == 409
    assert author.post(path + '/comments', json={'body': 'Уточнение для исполнителя'}).status_code == 201


def test_expired_deadline_is_escalated_again_after_rework(clients):
    author, _, manager, _, admin = clients
    ticket = create(author)
    with database() as db:
        row = db.get(Ticket, ticket['id'])
        row.resolution_due_at = now() - timedelta(hours=1)
        row.resolution_due_version = 1
        db.commit()
    assert dispatch(now()) == 1
    ticket = publish(author, manager, ticket)
    assert dispatch(now()) == 0
    returned = review(author, ticket, 'rework', 'Проблема не устранена').json()
    assert returned['resolution_overdue'] is True
    assert returned['resolution_due_at'] == ticket['resolution_due_at']
    assert dispatch(now()) == 1
    assert dispatch(now()) == 0
    attention = admin.get('/api/analytics/attention?reason=resolution_overdue').json()
    assert [item['id'] for item in attention['items']] == [ticket['id']]
    assert set_deadline(manager, ticket, (now() + timedelta(days=2)).isoformat() + 'Z',
                        version=returned['resolution_due_version']).status_code == 422
    assert set_deadline(manager, ticket, (now() + timedelta(days=2)).isoformat() + 'Z',
                        version=returned['resolution_due_version'], reason='План доработки').status_code == 200


def test_new_resolution_cycle_keeps_old_review_and_rejects_old_requests(clients):
    author, _, manager, _, _ = clients
    original = publish(author, manager, result='Первый результат')
    assert review(author, original, 'rework', 'Нужно доработать').status_code == 200
    updated = publish(author, manager, original, result='Исправленный результат')
    assert updated['resolution_review_version'] == original['resolution_review_version'] + 1
    assert review(author, original).status_code == 409
    assert review(author, original, 'rework', 'Нужно доработать').status_code == 409
    assert review(author, updated).status_code == 200
    detail = author.get(f"/api/tickets/{original['id']}").json()
    assert [(item['decision'], item['resolution_snapshot']) for item in detail['resolution_reviews']] == [
        ('confirm', 'Исправленный результат'), ('rework', 'Первый результат'),
    ]


def test_editing_published_result_invalidates_previous_revision(clients):
    author, _, manager, _, _ = clients
    original = publish(author, manager)
    path = f"/api/tickets/{original['id']}"
    payload = {'resolution': 'Уточнённый результат', 'expected_status': 'IMPLEMENTED',
               'expected_resolution_review_version': original['resolution_review_version']}
    updated = manager.patch(path, json=payload)
    assert updated.status_code == 200
    assert updated.json()['resolution_review_version'] == 2
    assert review(author, original).status_code == 409
    assert manager.patch(path, json=payload).status_code == 409
    current = updated.json()
    assert manager.patch(path, json={'resolution': current['resolution']}).json()['resolution_review_version'] == 2
    assert review(author, current).status_code == 200


@pytest.mark.parametrize('client_index,expected', [(1, 404), (2, 403), (3, 404), (4, 403)])
def test_only_author_can_review(clients, client_index, expected):
    author, _, manager, _, _ = clients
    ticket = publish(author, manager)
    assert review(clients[client_index], ticket).status_code == expected
    assert review(clients[client_index], ticket, 'rework', 'Не моя карточка').status_code == expected
    assert author.get(f"/api/tickets/{ticket['id']}").json()['resolution_reviews'] == []


@pytest.mark.parametrize('status', ['NEW', 'IN_REVIEW', 'IN_PROGRESS', 'AWAITING_CLARIFICATION', 'REJECTED', 'CLOSED'])
def test_review_is_not_available_outside_implemented(clients, status):
    author, _, _, _, _ = clients
    ticket = create(author)
    with database() as db:
        row = db.get(Ticket, ticket['id'])
        row.status = status
        row.resolution = 'Старый результат'
        db.commit()
    assert review(author, ticket).status_code == 422
    assert review(author, ticket, 'rework', 'Пояснение').status_code == 422
    current = author.get(f"/api/tickets/{ticket['id']}").json()
    assert current['status'] == status and current['resolution_confirmed_at'] is None


def test_legacy_implemented_result_can_be_confirmed(clients):
    author, _, _, _, _ = clients
    ticket = create(author)
    with database() as db:
        row = db.get(Ticket, ticket['id'])
        row.status = 'IMPLEMENTED'
        row.resolution = 'Результат до обновления'
        db.commit()
    response = review(author, ticket)
    assert response.status_code == 200
    assert response.json()['resolution_review_version'] == 0
    assert response.json()['resolution_confirmed_at']


def test_legacy_missing_result_can_only_be_returned(clients):
    author, _, _, _, _ = clients
    ticket = create(author)
    with database() as db:
        db.get(Ticket, ticket['id']).status = 'IMPLEMENTED'
        db.commit()
    assert review(author, ticket).status_code == 422
    assert review(author, ticket, 'rework', 'Результат не описан').status_code == 200


@pytest.mark.parametrize('client_index', [2, 4])
def test_author_with_manager_or_admin_role_can_confirm_own_ticket(clients, client_index):
    author = clients[client_index]
    ticket = publish(author, clients[4])
    assert review(author, ticket).status_code == 200


def test_shared_participant_cannot_review_and_gets_limited_notifications(clients):
    author, participant, manager, _, _ = clients
    response = author.post('/api/tickets', json={'type': 'PROBLEM', 'category_id': 1,
        'title': 'Общая проблема', 'description': 'Проблема нескольких сотрудников', 'is_shared': True})
    ticket = response.json()
    assert participant.post(f"/api/problems/{ticket['id']}/join").status_code == 200
    ticket = publish(author, manager, ticket)
    assert review(participant, ticket).status_code == 404
    assert review(author, ticket, 'rework', 'Конфиденциальное пояснение').status_code == 200
    notice = participant.get('/api/notifications').json()['items'][0]
    assert notice['kind'] == 'resolution_rework'
    assert notice['href'] == '/tickets#joined-problems'
    assert 'Конфиденциальное' not in str(notice)


def test_concurrent_opposite_decisions_accept_only_one(clients):
    author, _, manager, _, _ = clients
    ticket = publish(author, manager)
    second = TestClient(app)
    second.post('/api/auth/login', json={'email': 'u1@modus.demo', 'password': 'demo1234'})
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(review, author, ticket)
            other = workers.submit(review, second, ticket, 'rework', 'Доработать')
            assert sorted([first.result().status_code, other.result().status_code]) == [200, 409]
    finally:
        second.close()
    with database() as db:
        assert db.scalar(select(func.count()).select_from(ResolutionReview)) == 1
        saved = db.scalar(select(ResolutionReview))
        assert db.get(Ticket, ticket['id']).status == ('CLOSED' if saved.decision == 'confirm' else 'IN_PROGRESS')
        assert db.scalar(select(func.count()).select_from(Notification).where(
            Notification.kind.in_(['resolution_confirmed', 'resolution_rework']))) == 2


def test_confirmation_racing_result_edit_cannot_close_unread_result(clients):
    author, _, manager, _, _ = clients
    ticket = publish(author, manager)
    with ThreadPoolExecutor(max_workers=2) as workers:
        confirm = workers.submit(review, author, ticket)
        edit = workers.submit(manager.patch, f"/api/tickets/{ticket['id']}", json={
            'resolution': 'Другая версия результата', 'expected_status': 'IMPLEMENTED',
            'expected_resolution_review_version': ticket['resolution_review_version'],
        })
        assert sorted([confirm.result().status_code, edit.result().status_code]) == [200, 409]
    current = author.get(f"/api/tickets/{ticket['id']}").json()
    if current['status'] == 'CLOSED':
        assert current['resolution'] == ticket['resolution']
    else:
        assert current['resolution_review_version'] == 2 and current['resolution_confirmed_at'] is None


def test_stale_manager_form_cannot_republish_after_rework(clients):
    author, _, manager, _, _ = clients
    ticket = publish(author, manager)
    assert review(author, ticket, 'rework', 'Исправить').status_code == 200
    response = manager.patch(f"/api/tickets/{ticket['id']}", json={
        'status': 'IMPLEMENTED', 'resolution': ticket['resolution'],
        'expected_status': 'IMPLEMENTED',
        'expected_resolution_review_version': ticket['resolution_review_version'],
    })
    assert response.status_code == 409


def test_review_validation_does_not_change_state(clients):
    author, _, manager, _, _ = clients
    ticket = publish(author, manager)
    path = f"/api/tickets/{ticket['id']}/resolution-review"
    for payload in [
        {'decision': 'unknown', 'expected_version': 1},
        {'decision': 'confirm', 'expected_version': -1},
        {'decision': 'confirm'},
        {'decision': 'rework', 'expected_version': 1, 'explanation': 'x' * 10001},
    ]:
        assert author.post(path, json=payload).status_code == 422
    assert author.get(f"/api/tickets/{ticket['id']}").json()['status'] == 'IMPLEMENTED'


def test_review_migration_preserves_legacy_statuses_and_deadlines(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from app.database import BASE_DIR

    engine = create_engine(f"sqlite:///{tmp_path / 'reviews-migration.db'}")
    monkeypatch.setattr('app.database.engine', engine)
    config = Config(str(BASE_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(BASE_DIR / 'alembic'))
    try:
        command.upgrade(config, 'head')
        command.downgrade(config, '005')
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO users (id,email,password_hash,full_name,role,department,position,is_active,created_at) VALUES (1,'legacy@modus.demo','hash','Legacy','ADMIN','Office','',1,'2026-01-01')"))
            connection.execute(text("INSERT INTO categories (id,name,description,sla_first_response_hours,is_active,sort_order) VALUES (1,'Legacy','',48,1,0)"))
            for ticket_id, status in enumerate(['IMPLEMENTED', 'REJECTED', 'CLOSED'], 1):
                connection.execute(text("INSERT INTO tickets (id,number,type,category_id,title,description,author_id,is_shared,priority,status,resolution,created_at,updated_at,due_at,resolution_due_at,resolution_due_version) VALUES (:id,:number,'PROBLEM',1,'Legacy','Description',1,0,'HIGH',:status,'Original result','2026-01-01','2026-01-01','2026-01-03','2026-01-10',3)"),
                                   {'id': ticket_id, 'number': f'A-00{ticket_id}', 'status': status})
        command.upgrade(config, 'head')
        with engine.connect() as connection:
            rows = connection.execute(text('SELECT status,resolution,resolution_due_at,resolution_due_version,resolution_review_version,resolution_confirmed_at FROM tickets ORDER BY id')).all()
            assert [row[0] for row in rows] == ['IMPLEMENTED', 'REJECTED', 'CLOSED']
            assert all(tuple(row[1:]) == ('Original result', '2026-01-10', 3, 0, None) for row in rows)
            assert connection.scalar(text('SELECT count(*) FROM resolution_reviews')) == 0
    finally:
        engine.dispose()
