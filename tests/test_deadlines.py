from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from test_workflow import clients, create  # noqa: F401
from app.database import get_db
from app.main import app
from app.models import Notification, Ticket, TicketHistory, User, now
from app.services.deadlines import dispatch_deadline_notifications


@contextmanager
def database():
    generator = app.dependency_overrides[get_db]()
    try:
        yield next(generator)
    finally:
        generator.close()


def set_deadline(client, ticket, date, *, version=0, reason=""):
    return client.patch(f"/api/tickets/{ticket['id']}/resolution-deadline", json={
        "resolution_due_at": date, "expected_version": version, "reason": reason,
    })


def dispatch(timestamp, *, commit=True):
    with database() as db:
        sent = dispatch_deadline_notifications(db, timestamp)
        if commit:
            db.commit()
        else:
            db.rollback()
        return sent


def test_deadline_preserves_first_reply_and_records_timezone_and_reason(clients):
    author, _, manager, _, _ = clients
    ticket = create(author)
    assert ticket["resolution_due_at"] is None
    assert ticket["resolution_due_version"] == 0
    assert ticket["resolution_overdue"] is False
    future = now() + timedelta(days=3)
    local = future + timedelta(hours=5)
    response = set_deadline(manager, ticket, local.isoformat() + "+05:00")
    assert response.status_code == 200, response.text
    assert response.json()["resolution_due_at"] == future.isoformat()
    assert response.json()["due_at"] == ticket["due_at"]
    assert response.json()["first_response_at"] is None
    assert response.json()["resolution_due_version"] == 1
    later = future + timedelta(days=1)
    assert set_deadline(manager, ticket, later.isoformat() + "Z", version=1).status_code == 422
    assert set_deadline(manager, ticket, later.isoformat() + "Z", version=1, reason="  Ждём поставку  ").status_code == 200
    # Retrying exactly the same request is a no-op, including its old expected version.
    assert set_deadline(manager, ticket, later.isoformat() + "Z", version=1, reason="Ждём поставку").status_code == 200
    detail = author.get(f"/api/tickets/{ticket['id']}").json()
    history = [item for item in detail["history"] if item["field"] == "resolution_due_at"]
    assert len(history) == 2
    assert history[1]["old_value"] == future.isoformat()
    assert history[1]["new_value"] == later.isoformat()
    assert history[1]["reason"] == "Ждём поставку"
    assert history[1]["actor_id"] == 3
    assert detail["resolution_due_version"] == 2
    assert author.get('/api/notifications').json()['items'][0]['kind'] == 'deadline_changed'


def test_deadline_permissions_conflicts_and_atomic_failure(clients):
    author, other, manager, outside, admin = clients
    ticket = create(author)
    future = (now() + timedelta(days=3)).isoformat() + "Z"
    assert set_deadline(author, ticket, future).status_code == 403
    assert set_deadline(other, ticket, future).status_code == 403
    assert set_deadline(outside, ticket, future).status_code == 404
    assert set_deadline(manager, ticket, future).status_code == 200
    later = (now() + timedelta(days=4)).isoformat() + "Z"
    assert set_deadline(admin, ticket, later, version=0, reason="Уточнение").status_code == 409
    assert set_deadline(admin, ticket, later, version=1, reason=" ").status_code == 422
    assert set_deadline(admin, ticket, None, version=1).status_code == 422
    with database() as db:
        assert db.get(Ticket, ticket['id']).resolution_due_version == 1
        assert db.scalar(select(func.count()).select_from(TicketHistory)) == 1
    assert set_deadline(admin, ticket, None, version=1, reason="План пересматривается").status_code == 200
    detail = author.get(f"/api/tickets/{ticket['id']}").json()
    assert detail["resolution_due_at"] is None
    assert detail["history"][-1]["reason"] == "План пересматривается"


@pytest.mark.parametrize('date', ['invalid', '2030-01-01T12:00:00', '2020-01-01T12:00:00Z'])
def test_invalid_deadlines(clients, date):
    author, _, manager, _, _ = clients
    assert set_deadline(manager, create(author), date).status_code == 422


@pytest.mark.parametrize('status', ['IMPLEMENTED', 'REJECTED', 'CLOSED'])
def test_finished_tickets_do_not_accept_new_deadlines(clients, status):
    author, _, manager, _, _ = clients
    ticket = create(author)
    path = f"/api/tickets/{ticket['id']}"
    assert manager.patch(path, json={'status': 'IN_REVIEW'}).status_code == 200
    terminal = 'REJECTED' if status == 'CLOSED' else status
    assert manager.patch(path, json={'status': terminal, 'resolution': 'Итог'}).status_code == 200
    if status == 'CLOSED':
        assert manager.patch(path, json={'status': status}).status_code == 200
    assert set_deadline(manager, ticket, (now() + timedelta(days=2)).isoformat() + 'Z').status_code == 422


def test_resolution_overdue_survives_first_reply_and_attention_is_unique(clients):
    author, _, manager, _, admin = clients
    ticket = create(author)
    path = f"/api/tickets/{ticket['id']}"
    assert manager.post(path + '/comments', json={'body': 'Принято в работу'}).status_code == 201
    with database() as db:
        row = db.get(Ticket, ticket['id'])
        row.resolution_due_at = now() - timedelta(hours=1)
        row.assignee_id = None
        db.commit()
    detail = author.get(path).json()
    assert detail['resolution_overdue'] is True and detail['overdue'] is False
    response = admin.get('/api/analytics/attention?reason=resolution_overdue').json()
    assert response['counts']['resolution_overdue'] == 1
    assert response['counts']['total'] == response['total'] == 1
    assert response['items'][0]['attention_reasons'] == ['unassigned', 'resolution_overdue']
    manager.patch(path, json={'status': 'REJECTED', 'resolution': 'Итог'})
    assert admin.get('/api/analytics/attention?reason=resolution_overdue').json()['total'] == 0


def test_reminder_escalation_deduplication_and_new_deadline_version(clients):
    author, _, manager, _, admin = clients
    ticket = create(author)
    stamp = now()
    due = stamp + timedelta(hours=20)
    assert set_deadline(manager, ticket, due.isoformat() + 'Z').status_code == 200
    assert dispatch(stamp - timedelta(hours=5)) == 0
    assert dispatch(stamp) == 1
    assert dispatch(stamp + timedelta(minutes=1)) == 0
    assert manager.get('/api/notifications').json()['items'][0]['kind'] == 'resolution_reminder'
    assert dispatch(due) == 1
    assert dispatch(due + timedelta(days=1)) == 0
    assert admin.get('/api/notifications').json()['items'][0]['kind'] == 'resolution_overdue'
    later = due + timedelta(days=2)
    assert set_deadline(manager, ticket, later.isoformat() + 'Z', version=1, reason='Перенос').status_code == 200
    assert dispatch(later - timedelta(hours=2)) == 1
    assert dispatch(later) == 1
    with database() as db:
        notices = db.scalars(select(Notification).where(Notification.deduplication_key.is_not(None))).all()
        assert len(notices) == len({notice.deduplication_key for notice in notices}) == 4


def test_deadline_notifications_handle_reassignment_inactive_users_and_completion(clients):
    author, _, manager, _, admin = clients
    ticket = create(author)
    stamp = now()
    due = stamp + timedelta(hours=2)
    set_deadline(manager, ticket, due.isoformat() + 'Z')
    assert dispatch(stamp) == 1
    path = f"/api/tickets/{ticket['id']}"
    assert admin.patch(path, json={'assignee_id': 4}).status_code == 200
    assert dispatch(stamp) == 1
    with database() as db:
        db.get(User, 5).is_active = False
        db.commit()
    assert dispatch(due) == 0
    with database() as db:
        db.get(User, 5).is_active = True
        db.get(Ticket, ticket['id']).status = 'IMPLEMENTED'
        db.commit()
    assert dispatch(due) == 0


def test_notification_rollback_can_be_retried(clients):
    author, _, manager, _, _ = clients
    ticket = create(author)
    stamp = now()
    set_deadline(manager, ticket, (stamp + timedelta(hours=2)).isoformat() + 'Z')
    assert dispatch(stamp, commit=False) == 1
    assert dispatch(stamp) == 1
    assert dispatch(stamp) == 0


def test_two_notification_workers_do_not_duplicate(clients):
    author, _, manager, _, _ = clients
    ticket = create(author)
    stamp = now()
    set_deadline(manager, ticket, (stamp + timedelta(hours=2)).isoformat() + 'Z')
    with database() as db:
        sessions = sessionmaker(db.get_bind())

    def run_worker(_):
        with sessions() as session:
            sent = dispatch_deadline_notifications(session, stamp)
            session.commit()
            return sent

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(workers.map(run_worker, range(2))) == 1


def test_concurrent_deadline_edits_preserve_one_history_entry(clients):
    author, _, manager, _, admin = clients
    ticket = create(author)
    future = now() + timedelta(days=2)

    def edit(client):
        return set_deadline(client, ticket, future.isoformat() + 'Z').status_code

    # Identical retries are accepted, but cannot create a second revision.
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(edit, [manager, admin]))
    assert all(code in (200, 409) for code in responses)
    assert 200 in responses
    detail = author.get(f"/api/tickets/{ticket['id']}").json()
    assert detail['resolution_due_version'] == 1
    assert len([item for item in detail['history'] if item['field'] == 'resolution_due_at']) == 1


def test_migration_upgrades_legacy_rows_and_fresh_database(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text
    from app.database import BASE_DIR

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    monkeypatch.setattr('app.database.engine', engine)
    config = Config(str(BASE_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(BASE_DIR / 'alembic'))
    try:
        command.upgrade(config, 'head')
        with engine.begin() as connection:
            # A minimal legacy row also catches accidental NOT NULL backfills.
            connection.execute(text("INSERT INTO users (id,email,password_hash,full_name,role,department,position,is_active,created_at) VALUES (1,'legacy@modus.demo','hash','Legacy','ADMIN','Office','',1,'2026-01-01')"))
            connection.execute(text("INSERT INTO categories (id,name,description,sla_first_response_hours,is_active,sort_order) VALUES (1,'Legacy','',48,1,0)"))
            connection.execute(text("INSERT INTO tickets (id,number,type,category_id,title,description,author_id,is_shared,priority,status,created_at,updated_at,due_at) VALUES (1,'A-001','PROBLEM',1,'Legacy','Old description',1,0,'HIGH','NEW','2026-01-01','2026-01-01','2026-01-03')"))
            connection.execute(text("INSERT INTO ticket_history (id,ticket_id,actor_id,field,new_value,created_at) VALUES (1,1,1,'priority','HIGH','2026-01-01')"))
            connection.execute(text("INSERT INTO notifications (id,user_id,ticket_id,kind,created_at) VALUES (1,1,1,'created','2026-01-01')"))
        command.downgrade(config, '004')
        assert 'resolution_due_at' not in {item['name'] for item in inspect(engine).get_columns('tickets')}
        command.upgrade(config, 'head')
        with engine.connect() as connection:
            row = connection.execute(text('SELECT description, due_at, resolution_due_at, resolution_due_version FROM tickets')).one()
            assert tuple(row) == ('Old description', '2026-01-03', None, 0)
            assert connection.scalar(text('SELECT count(*) FROM ticket_history')) == 1
            assert connection.scalar(text('SELECT count(*) FROM notifications')) == 1
            assert connection.scalar(text('SELECT reason FROM ticket_history')) is None
            assert connection.scalar(text('SELECT deduplication_key FROM notifications')) is None
    finally:
        engine.dispose()


def test_background_worker_starts_without_browser_and_stops(monkeypatch):
    monkeypatch.setattr("app.services.integrations.run_cycle", lambda: None)
    import asyncio
    from app.main import lifespan

    completed = []
    monkeypatch.setattr('app.services.deadlines._dispatch_batch', lambda: completed.append(True))

    async def run():
        async with lifespan(app):
            for _ in range(100):
                if completed:
                    break
                await asyncio.sleep(0.01)
            assert completed

    asyncio.run(run())


def test_background_worker_retries_after_failure(monkeypatch):
    monkeypatch.setattr("app.services.integrations.run_cycle", lambda: None)
    import asyncio
    from app.main import lifespan

    attempts = []

    def batch():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError('Temporary database failure')

    monkeypatch.setattr('app.services.deadlines._dispatch_batch', batch)
    monkeypatch.setattr('app.services.deadlines.CHECK_INTERVAL_SECONDS', 0.01)

    async def run():
        async with lifespan(app):
            for _ in range(100):
                if len(attempts) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(attempts) >= 2

    asyncio.run(run())
