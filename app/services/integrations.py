import asyncio
import json
import os
import secrets
from datetime import timedelta
from sqlalchemy import select, update, or_
from ..database import SessionLocal
from ..models import User, Ticket, Notification, now
from ..integration_models import IntegrationSettings, DirectoryEntry, BitrixTask, IntegrationJob
from .bitrix_client import BitrixError, client, scope


def settings(db):
    item = db.get(IntegrationSettings, 1)
    if not item:
        item = IntegrationSettings(id=1, instance_id=secrets.token_hex(12), mode='off')
        db.add(item)
        db.flush()
    return item


def log(db, realm, kind, state, message):
    item = IntegrationJob(scope=realm, kind=kind, state=state, message=message)
    db.add(item)
    return item


def enqueue_notification(db, notification):
    cfg = db.get(IntegrationSettings, 1)
    if not cfg or cfg.mode == 'off': return
    try: realm = scope(cfg.mode)
    except BitrixError: return
    db.add(IntegrationJob(scope=realm, kind='notification', notification_id=notification.id))


def active(value):
    return value is True or str(value).upper() in ('Y', '1', 'TRUE')


def directory_sync(db, api, realm):
    # Fetch both complete snapshots before touching accounts; failed pages cannot disable users.
    departments = api.all('department.get')
    employees = api.all('user.get')
    from ..integration_models import BitrixBot
    bot_ids = set(db.scalars(select(BitrixBot.external_id).where(BitrixBot.scope == realm)))
    employees = [u for u in employees if int(u['ID']) not in bot_ids and u.get('EXTERNAL_AUTH_ID') not in ('bot', 'imconnector')]
    names = {int(d['ID']): str(d.get('NAME', '')) for d in departments}
    seen = set()
    for kind, rows in [('department', departments), ('employee', employees)]:
        for raw in rows:
            ext_id = int(raw['ID'])
            seen.add((kind, ext_id))
            entry = db.scalar(select(DirectoryEntry).where(DirectoryEntry.scope == realm, DirectoryEntry.kind == kind, DirectoryEntry.external_id == ext_id))
            if not entry:
                entry = DirectoryEntry(scope=realm, kind=kind, external_id=ext_id, payload='{}')
                db.add(entry)
            # Store only the fields used by our UI, not personal details returned by user.get.
            if kind == 'department':
                data = {'name': str(raw.get('NAME', '')), 'parent_id': int(raw.get('PARENT') or 0), 'head_id': int(raw.get('UF_HEAD') or 0)}
            else:
                deps = [int(i) for i in (raw.get('UF_DEPARTMENT') or [])]
                data = {'name': ' '.join(str(raw.get(k) or '') for k in ('NAME', 'LAST_NAME')).strip() or f'Сотрудник {ext_id}',
                        'email': str(raw.get('EMAIL') or '').lower(), 'active': active(raw.get('ACTIVE')),
                        'department_ids': deps, 'position': str(raw.get('WORK_POSITION') or '')}
                user = db.get(User, entry.user_id) if entry.user_id else None
                if not user:
                    # Do not silently link by email: that could grant the wrong person notifications/access.
                    email = data['email']
                    if not email or db.scalar(select(User.id).where(User.email == email)):
                        email = f'bitrix-{settings(db).instance_id}-{realm == "mock"}-{ext_id}@import.invalid'
                    from ..security import passwords
                    user = User(email=email, full_name=data['name'][:255], role='EMPLOYEE', department='Без подразделения',
                                password_hash=passwords.hash(secrets.token_urlsafe(32)), is_active=data['active'])
                    db.add(user)
                    db.flush()
                    entry.user_id, entry.managed = user.id, True
                if entry.managed:
                    user.full_name = data['name'][:255]
                    user.department = (names.get(deps[0], 'Без подразделения') if deps else 'Без подразделения')[:150]
                    user.position = data['position'][:150]
                    # Never lock an administrator out through a directory sync.
                    if user.role != 'ADMIN': user.is_active = data['active']
            entry.payload = json.dumps(data, ensure_ascii=False)
            entry.synced_at = now()
    for entry in db.scalars(select(DirectoryEntry).where(DirectoryEntry.scope == realm)):
        if (entry.kind, entry.external_id) not in seen:
            data = json.loads(entry.payload)
            data['active'] = False
            data['missing'] = True
            entry.payload = json.dumps(data, ensure_ascii=False)
            if entry.kind == 'employee' and entry.managed:
                user = db.get(User, entry.user_id)
                if user and user.role != 'ADMIN': user.is_active = False
    settings(db).synced_at = now()
    return f'Обновлено сотрудников: {len(employees)}, подразделений: {len(departments)}'


def employee_link(db, realm, user_id):
    row = db.scalar(select(DirectoryEntry).where(DirectoryEntry.scope == realm, DirectoryEntry.kind == 'employee', DirectoryEntry.user_id == user_id))
    if not row or not json.loads(row.payload).get('active'):
        raise BitrixError('EMPLOYEE_NOT_LINKED_OR_INACTIVE')
    return row


def public_url():
    from urllib.parse import urlsplit
    value = os.getenv('MODUS_PUBLIC_URL', 'https://modus-trade.makquiz.site').rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or any(c in value for c in '[]\n\r'):
        raise BitrixError('INVALID_PUBLIC_URL')
    from ipaddress import ip_address
    host = (parsed.hostname or '').rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')) or parsed.query or parsed.fragment:
        raise BitrixError('INVALID_PUBLIC_URL')
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise BitrixError('INVALID_PUBLIC_URL')
    return value


def process_notification(db, job, api):
    job.attempts += 1
    n = db.get(Notification, job.notification_id)
    recipient = db.get(User, n.user_id) if n else None
    if not recipient or not recipient.is_active:
        job.state, job.message = 'skipped', 'Получатель неактивен'
        return
    try: person = employee_link(db, job.scope, n.user_id)
    except BitrixError:
        job.state, job.message = 'skipped', 'Нет активной связи с сотрудником Битрикс24'
        return
    from .tickets import visible, joined_ticket
    ticket = db.get(Ticket, n.ticket_id)
    allowed = db.scalar(visible(recipient).where(Ticket.id == n.ticket_id)) is not None
    if not allowed and not joined_ticket(db, recipient, n.ticket_id):
        job.state, job.message = 'skipped', 'Доступ к обращению отозван'
        return
    from .notifications import LABELS
    message = f'{LABELS.get(n.kind, "Обновление обращения")} · {ticket.number}. [URL={public_url()}/tickets/{ticket.id}]Открыть обращение[/URL]'
    # Commit before a non-idempotent network write. On restart "sending" becomes uncertain, never blind resend.
    job.state = 'sending'
    db.commit()
    result = api.call('im.notify.personal.add', {'USER_ID': person.external_id, 'MESSAGE': message})['result']
    if not result: raise BitrixError('NOTIFICATION_NOT_DELIVERED')
    job.state, job.message = 'sent', 'Битрикс24 — отправлено' if job.scope != 'mock' else 'МОК — доставка смоделирована'


def apply_completion(db, link, text):
    if link.applied_completion: return
    ticket = db.scalar(select(Ticket).where(Ticket.id == link.ticket_id).with_for_update())
    if ticket.status not in ('IN_PROGRESS', 'IN_REVIEW'):
        link.state, link.error = 'review_required', 'Статус обращения изменён в Модусе. Проверьте результат вручную.'
        return
    if not text.strip():
        link.state, link.error = 'needs_result', 'Задача завершена без результата. Закрепите результат в Битрикс24.'
        return
    # Compare-and-swap protects SQLite as well as databases with row locks.
    locked = db.execute(update(Ticket).where(Ticket.id == ticket.id, Ticket.status == ticket.status,
        Ticket.resolution_review_version == ticket.resolution_review_version).values(updated_at=Ticket.updated_at))
    if locked.rowcount != 1:
        raise BitrixError("TICKET_CHANGED_RETRY")
    db.refresh(ticket)
    actor = db.get(User, link.actor_id)
    from .tickets import change, set_status
    from .notifications import notify
    from .live import ticket_changed
    change(db, ticket, actor, 'resolution', text[:20000])
    set_status(db, ticket, actor, 'IMPLEMENTED')
    ticket.resolution_review_version += 1
    ticket.resolution_confirmed_at = None
    notify(db, ticket, None, 'result')
    ticket_changed(db, ticket, 'updated')
    link.applied_completion, link.state, link.error = True, 'linked', None
    log(db, link.scope, 'task_result', 'sent', f'{ticket.number}: результат передан на проверку автору')


def process_task(db, link, api):
    ticket = db.get(Ticket, link.ticket_id)
    link.checked_at = now()
    if not link.external_id:
        if ticket.status != 'IN_PROGRESS': return
        person = employee_link(db, link.scope, ticket.assignee_id)
        if link.scope != 'mock':
            found = api.call('tasks.task.list', {'filter': {'XML_ID': link.xml_id}, 'select': ['ID', 'XML_ID']})['result']['tasks']
            if len(found) > 1: raise BitrixError('MULTIPLE_TASKS_REQUIRE_REVIEW')
            if found: link.external_id = int(found[0]['id'])
        if not link.external_id:
            if link.state in ('creating', 'uncertain'):
                link.state, link.error = 'uncertain', 'Ответ на создание потерян. Проверяем по XML_ID; повторное создание заблокировано.'
                return
            fields = {'TITLE': f'{ticket.number}: {ticket.title}', 'DESCRIPTION': f'Обращение Модус: {public_url()}/tickets/{ticket.id}',
                      'RESPONSIBLE_ID': person.external_id, 'XML_ID': link.xml_id}
            if ticket.resolution_due_at: fields['DEADLINE'] = ticket.resolution_due_at.isoformat() + '+00:00'
            link.state = 'creating'
            db.commit()
            try:
                result = api.call('tasks.task.add', {'fields': fields})['result']['task']
                link.external_id = int(result['id'])
            except BitrixError as e:
                link.state = 'uncertain' if e.uncertain else 'error'
                raise
            link.remote_assignee = str(person.external_id)
            link.remote_deadline = fields.get('DEADLINE')
            log(db, link.scope, 'task_create', 'sent', f'{ticket.number}: создана задача #{link.external_id}' + (' (МОК)' if link.scope == 'mock' else ''))
        link.state, link.error = 'linked', None
        db.commit()
    if link.scope == 'mock':
        link.remote_status = link.remote_status or '2'
        return
    task = api.call('tasks.task.get', {'taskId': link.external_id})['result']['task']
    link.remote_status = str(task.get('status', ''))
    link.remote_assignee = str(task.get('responsibleId', ''))
    link.remote_deadline = task.get('deadline')
    link.error = None
    if link.remote_status == '5' and not link.applied_completion:
        results = api.call('tasks.task.result.list', {'taskId': link.external_id})['result']
        text = '\n\n'.join(str(r.get('text') or '') for r in results if isinstance(r, dict)).strip()
        apply_completion(db, link, text)
    elif link.applied_completion and ticket.status == 'IN_PROGRESS':
        link.state, link.error = 'review_required', 'Автор вернул решение на доработку. Возобновите связанную задачу в Битрикс24.'
        # A stale completed task must never overwrite the author's rework.
        if link.remote_status != '5': link.applied_completion = False
    elif link.remote_status != '5':
        link.state = 'linked'


def run_cycle():
    # Database lease prevents multiple uvicorn workers from sending the same outbox entry.
    with SessionLocal() as db:
        cfg = db.get(IntegrationSettings, 1)
        if not cfg or cfg.mode == 'off': return
        claimed = db.execute(update(IntegrationSettings).where(IntegrationSettings.id == 1,
            or_(IntegrationSettings.lease_until.is_(None), IntegrationSettings.lease_until < now())).values(lease_until=now() + timedelta(minutes=10)))
        db.commit()
        if not claimed.rowcount: return
        try:
            mode = cfg.mode
            realm, api = scope(mode), client(mode)
            from .bitrix_bot import run_bot
            run_bot(db, realm, api)
            # A process died after making an irreversible request. Do not resend blindly.
            for job in db.scalars(select(IntegrationJob).where(IntegrationJob.scope == realm, IntegrationJob.state == 'sending')):
                job.state, job.message = 'uncertain', 'Ответ на отправку потерян. Автоповтор отключён, проверьте портал.'
            last_sync = db.scalar(select(IntegrationJob).where(IntegrationJob.scope == realm, IntegrationJob.kind == 'directory').order_by(IntegrationJob.id.desc()))
            if not last_sync or (last_sync.state not in ('pending', 'error') and last_sync.created_at < now() - timedelta(hours=1)):
                log(db, realm, 'directory', 'pending', 'Плановая синхронизация')
            db.flush()
            jobs = db.scalars(select(IntegrationJob).where(IntegrationJob.scope == realm, IntegrationJob.state.in_(['pending', 'error']),
                    IntegrationJob.next_at <= now(), IntegrationJob.attempts < 5).order_by(IntegrationJob.id).limit(5)).all()
            for job in jobs:
                try:
                    if job.kind == 'directory':
                        job.attempts += 1
                        job.message = directory_sync(db, api, realm)
                        job.state = 'sent'
                    elif job.kind == 'notification': process_notification(db, job, api)
                except BitrixError as e:
                    job.state = 'uncertain' if e.uncertain and job.state == 'sending' else 'error'
                    job.message = e.code
                    job.next_at = now() + timedelta(seconds=min(1800, 30 * 2 ** job.attempts))
                job.updated_at = now()
                db.commit()
            links = db.scalars(select(BitrixTask).where(BitrixTask.scope == realm, BitrixTask.enabled.is_(True),
                    or_(BitrixTask.checked_at.is_(None), BitrixTask.checked_at < now() - timedelta(seconds=60)))
                    .order_by(BitrixTask.checked_at, BitrixTask.id).limit(5)).all()
            for link in links:
                try: process_task(db, link, api)
                except BitrixError as e:
                    link.error = e.code
                    if link.state not in ('uncertain', 'creating'): link.state = 'error'
                db.commit()
        except BitrixError as e:
            db.rollback()
            cfg = db.get(IntegrationSettings, 1)
            cfg.health = e.code
        except Exception:
            db.rollback()
            cfg = db.get(IntegrationSettings, 1)
            cfg.health = 'INTERNAL_EXCHANGE_ERROR: проверьте журнал и конфигурацию'
        finally:
            db.execute(update(IntegrationSettings).where(IntegrationSettings.id == 1).values(lease_until=None))
            db.commit()


async def integration_loop(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_cycle)
        except Exception:
            import logging
            logging.getLogger(__name__).error("Bitrix worker unavailable; retry in 10 seconds")
        try: await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError: pass
