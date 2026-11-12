import json
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from ..security import Admin, CurrentUser, DB
from ..models import User, Ticket, Notification, now
from ..integration_models import IntegrationSettings, DirectoryEntry, IntegrationJob, BitrixTask
from ..services.integrations import settings, log, apply_completion, public_url
from ..services.bitrix_client import client, scope, BitrixError
from ..services.tickets import get_ticket, require_manager

router = APIRouter(prefix='/api/integrations/bitrix', tags=['integrations'])


def realm_for(db):
    try: return scope(settings(db).mode)
    except BitrixError as e: raise HTTPException(422, e.code)


def ensure_idle(cfg):
    if cfg.lease_until and cfg.lease_until > now():
        raise HTTPException(409, 'Идёт обмен с Битрикс24. Повторите через несколько секунд.')


@router.get('')
def overview(db: DB, user: Admin):
    cfg = settings(db)
    try: realm = scope(cfg.mode)
    except BitrixError: realm = 'not-configured'
    data = {'mode': cfg.mode, 'portal': realm, 'synced_at': cfg.synced_at, 'checked_at': cfg.checked_at,
            'health': cfg.health, 'public_url': public_url(), 'directory': [], 'jobs': []}
    for row in db.scalars(select(DirectoryEntry).where(DirectoryEntry.scope == realm).order_by(DirectoryEntry.kind, DirectoryEntry.external_id)):
        local = db.get(User, row.user_id) if row.user_id else None
        data['directory'].append({'id': row.id, 'kind': row.kind, 'external_id': row.external_id, 'user_id': row.user_id,
            'local_name': local.full_name if local else None, 'managed': row.managed, 'synced_at': row.synced_at, **json.loads(row.payload)})
    for j in db.scalars(select(IntegrationJob).where(IntegrationJob.scope == realm).order_by(IntegrationJob.id.desc()).limit(100)):
        n = db.get(Notification, j.notification_id) if j.notification_id else None
        data['jobs'].append({'id': j.id, 'kind': j.kind, 'state': j.state, 'attempts': j.attempts, 'message': j.message,
            'created_at': j.created_at, 'ticket_id': n.ticket_id if n else None,
            'recipient': db.get(User, n.user_id).full_name if n else None, 'internal_created': n is not None})
    db.commit()
    return data


class Mode(BaseModel):
    mode: Literal['off', 'mock', 'real']


@router.put('/mode')
def mode(data: Mode, db: DB, user: Admin):
    cfg = settings(db)
    ensure_idle(cfg)
    try: scope(data.mode)
    except BitrixError as e: raise HTTPException(422, e.code)
    cfg.mode = data.mode
    cfg.health = 'Режим изменён. Проверьте соединение.'
    cfg.synced_at = None
    db.commit()
    return {'mode': cfg.mode}


@router.post('/check')
def check(db: DB, user: Admin):
    cfg = settings(db)
    try:
        api = client(cfg.mode)
        results = {}
        for method in ('profile', 'user.get', 'department.get', 'tasks.task.list', 'scope'):
            try:
                response = api.call(method)
                results[method] = response['result'] if method == 'scope' else 'OK'
            except BitrixError as e: results[method] = e.code
        cfg.health = json.dumps(results, ensure_ascii=False)
    except BitrixError as e:
        cfg.health = e.code
    cfg.checked_at = now()
    db.commit()
    return {'health': cfg.health}


@router.post('/sync')
def sync(db: DB, user: Admin):
    cfg = settings(db)
    ensure_idle(cfg)
    if cfg.mode == 'off': raise HTTPException(422, 'Сначала включите интеграцию')
    realm = realm_for(db)
    if not db.scalar(select(IntegrationJob.id).where(IntegrationJob.scope == realm, IntegrationJob.kind == 'directory', IntegrationJob.state.in_(['pending', 'sending']))):
        log(db, realm, 'directory', 'pending', 'Ручная синхронизация')
    db.commit()
    return {'queued': True}


class LinkUser(BaseModel):
    user_id: int


@router.put('/directory/{entry_id}/link')
def link_user(entry_id: int, data: LinkUser, db: DB, user: Admin):
    ensure_idle(settings(db))
    row = db.get(DirectoryEntry, entry_id)
    local = db.get(User, data.user_id)
    if not row or row.kind != 'employee' or row.scope != realm_for(db): raise HTTPException(404, 'Сотрудник не найден')
    if not local or not local.is_active: raise HTTPException(422, 'Выберите активную учётную запись')
    duplicate = db.scalar(select(DirectoryEntry.id).where(DirectoryEntry.scope == row.scope, DirectoryEntry.user_id == local.id, DirectoryEntry.id != row.id))
    if duplicate: raise HTTPException(409, 'Эта учётная запись уже связана с другим сотрудником')
    row.user_id, row.managed = local.id, False
    log(db, row.scope, 'account_link', 'sent', f'Администратор #{user.id}: сотрудник Битрикс24 #{row.external_id} связан с учётной записью #{local.id}')
    db.commit()
    return {'ok': True}


@router.post('/jobs/{job_id}/retry')
def retry(job_id: int, db: DB, user: Admin):
    ensure_idle(settings(db))
    job = db.get(IntegrationJob, job_id)
    if not job or job.scope != realm_for(db): raise HTTPException(404)
    if job.state not in ('error', 'skipped'): raise HTTPException(422, 'Повтор возможен только для ошибки или пропущенной доставки')
    job.state, job.attempts, job.next_at = 'pending', 0, now()
    db.commit()
    return {'queued': True}


def task_data(db, ticket_id):
    cfg = settings(db)
    try: realm = scope(cfg.mode)
    except BitrixError: realm = 'not-configured'
    link = db.scalar(select(BitrixTask).where(BitrixTask.scope == realm, BitrixTask.ticket_id == ticket_id))
    data = jsonable_encoder(link) if link else {'enabled': False, 'state': 'off'}
    data.update(mode=cfg.mode, url=(f'{realm}/company/personal/user/{int(link.remote_assignee or 1)}/tasks/task/view/{link.external_id}/'
                    if link and link.external_id and cfg.mode == 'real' else None))
    if link and link.remote_assignee:
        person = db.scalar(select(DirectoryEntry).where(DirectoryEntry.scope == realm, DirectoryEntry.kind == 'employee', DirectoryEntry.external_id == int(link.remote_assignee)))
        data['assignee_name'] = json.loads(person.payload)['name'] if person else f'#{link.remote_assignee}'
    return data


@router.get('/tickets/{ticket_id}')
def task_get(ticket_id: int, db: DB, user: CurrentUser):
    get_ticket(db, user, ticket_id)
    data = task_data(db, ticket_id)
    db.commit()
    return data


class Toggle(BaseModel):
    enabled: bool


@router.put('/tickets/{ticket_id}')
def task_toggle(ticket_id: int, data: Toggle, db: DB, user: CurrentUser):
    ticket = get_ticket(db, user, ticket_id, lock=True)
    require_manager(user, ticket)
    cfg = settings(db)
    ensure_idle(cfg)
    if cfg.mode == 'off': raise HTTPException(422, 'Администратор должен включить интеграцию')
    if ticket.status in ('CLOSED', 'REJECTED', 'IMPLEMENTED'): raise HTTPException(422, 'Обращение уже завершено')
    realm = realm_for(db)
    row = db.scalar(select(BitrixTask).where(BitrixTask.scope == realm, BitrixTask.ticket_id == ticket_id))
    if not row:
        row = BitrixTask(scope=realm, ticket_id=ticket_id, actor_id=user.id, xml_id=f'modus-{cfg.instance_id}-{secrets_suffix(realm)}-{ticket_id}')
        db.add(row)
    row.enabled, row.checked_at = data.enabled, None
    db.commit()
    return task_data(db, ticket_id)


def secrets_suffix(realm):
    import hashlib
    return hashlib.sha256(realm.encode()).hexdigest()[:12]


class MockResult(BaseModel):
    result: str = Field(min_length=3, max_length=20000)


@router.post('/tickets/{ticket_id}/mock-complete')
def mock_complete(ticket_id: int, data: MockResult, db: DB, user: Admin):
    if settings(db).mode != 'mock': raise HTTPException(422, 'Доступно только в МОК-режиме')
    get_ticket(db, user, ticket_id)
    link = db.scalar(select(BitrixTask).where(BitrixTask.scope == 'mock', BitrixTask.ticket_id == ticket_id))
    if not link or not link.external_id: raise HTTPException(422, 'Сначала создайте МОК-задачу')
    link.remote_status = '5'
    apply_completion(db, link, data.result)
    db.commit()
    return {'ok': True}
