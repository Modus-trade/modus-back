import json
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from test_workflow import clients, create  # noqa: F401
from app.models import User, Ticket, Notification, now
from app.integration_models import IntegrationJob, DirectoryEntry, BitrixTask
from app.services import integrations as service
from app.services.bitrix_client import MockClient, BitrixClient, BitrixError

BASE = '/api/integrations/bitrix'


def setup_mock(admin):
    assert admin.put(BASE + '/mode', json={'mode': 'mock'}).status_code == 200
    assert admin.post(BASE + '/sync').status_code == 200
    service.run_cycle()
    return admin.get(BASE).json()


def test_admin_access_modes_directory_idempotence(clients):
    author, _, manager, _, admin = clients
    assert author.get(BASE).status_code == 403
    assert manager.post(BASE + '/sync').status_code == 403
    assert admin.post(BASE + '/sync').status_code == 422
    data = setup_mock(admin)
    employees = [e for e in data['directory'] if e['kind'] == 'employee']
    assert len(employees) == 2
    assert all(e['active'] for e in employees)
    users = admin.get('/api/users').json()
    assert all(u['role'] == 'EMPLOYEE' for u in users if u['id'] in [e['user_id'] for e in employees])
    assert employees[0]['user_id'] in [u['id'] for u in admin.get('/api/assignees').json()]
    admin.post(BASE + '/sync')
    service.run_cycle()
    assert len(admin.get('/api/users').json()) == len(users)
    assert all('password' not in str(e) for e in data['directory'])
    assert admin.put(BASE + '/mode', json={'mode':'off'}).status_code == 200
    assert admin.get(BASE).json()['directory'] == []
    assert employees[0]['user_id'] in [u['id'] for u in admin.get('/api/assignees').json()]


def test_link_conflict_and_no_email_auto_link(clients):
    *_, admin = clients
    data = setup_mock(admin)
    people = [e for e in data['directory'] if e['kind'] == 'employee']
    assert admin.put(f"{BASE}/directory/{people[0]['id']}/link",json={'user_id':1}).status_code == 200
    assert admin.put(f"{BASE}/directory/{people[1]['id']}/link",json={'user_id':1}).status_code == 409
    admin.post(BASE + '/sync'); service.run_cycle()
    with service.SessionLocal() as db:
        assert db.get(User,1).full_name == 'Пользователь 1'
        assert db.get(User,1).role == 'EMPLOYEE'


def test_assignment_and_mock_round_trip_author_confirmation(clients):
    author, other, _, _, admin = clients
    data = setup_mock(admin)
    person = next(e for e in data['directory'] if e['kind'] == 'employee')
    # Explicitly link a real local employee account to an imported directory identity.
    admin.put(f"{BASE}/directory/{person['id']}/link",json={'user_id':2})
    t = create(author)
    tid = t['id']
    assert other.put(f'{BASE}/tickets/{tid}',json={'enabled':True}).status_code == 404
    assert admin.patch(f'/api/tickets/{tid}',json={'assignee_id':2}).status_code == 200
    assert other.get(f'/api/tickets/{tid}').status_code == 200
    assert other.put(f'{BASE}/tickets/{tid}',json={'enabled':True}).status_code == 200
    service.run_cycle()
    assert not admin.get(f'{BASE}/tickets/{tid}').json()['external_id']
    assert other.patch(f'/api/tickets/{tid}',json={'status':'IN_PROGRESS'}).status_code == 200
    with service.SessionLocal() as db:
        link = db.scalar(select(BitrixTask)); link.checked_at=None; db.commit()
    service.run_cycle()
    link = admin.get(f'{BASE}/tickets/{tid}').json()
    assert link['external_id'] == tid and link['mode']=='mock' and link['url'] is None
    assert admin.post(f'{BASE}/tickets/{tid}/mock-complete',json={'result':'Исправлена причина проблемы'}).status_code == 200
    detail = author.get(f'/api/tickets/{tid}').json()
    assert detail['status'] == 'IMPLEMENTED'
    assert detail['resolution_review_version'] == 1
    admin.post(f'{BASE}/tickets/{tid}/mock-complete',json={'result':'Повтор'})
    assert author.get(f'/api/tickets/{tid}').json()['resolution_review_version'] == 1
    assert author.post(f'/api/tickets/{tid}/resolution-review',json={'decision':'confirm','expected_version':1}).status_code == 200
    assert author.get(f'/api/tickets/{tid}').json()['status']=='CLOSED'


def test_notifications_transaction_delivery_privacy_and_restart(clients, monkeypatch):
    author, _, _, _, admin = clients
    data=setup_mock(admin)
    person=next(e for e in data['directory'] if e['kind']=='employee')
    admin.put(f"{BASE}/directory/{person['id']}/link",json={'user_id':1})
    t=create(author)
    admin.post(f"/api/tickets/{t['id']}/comments",json={'body':'PRIVATE COMMENT: do not export'})
    captured=[]
    class Capture(MockClient):
        def call(self, method, params=None):
            if method=='im.notify.personal.add': captured.append(params)
            return super().call(method,params)
    monkeypatch.setattr(service,'client',lambda mode:Capture())
    service.run_cycle()
    assert len(captured)==1
    assert 'PRIVATE' not in captured[0]['MESSAGE']
    assert f"/tickets/{t['id']}" in captured[0]['MESSAGE']
    service.run_cycle(); assert len(captured)==1
    with service.SessionLocal() as db:
        before=db.scalar(select(func.count()).select_from(IntegrationJob))
        from app.services.notifications import notify
        notify(db,db.get(Ticket,t['id']),5,'comment')
        db.rollback()
        assert db.scalar(select(func.count()).select_from(IntegrationJob))==before
        sent=db.scalar(select(IntegrationJob).where(IntegrationJob.state=='sent',IntegrationJob.kind=='notification'))
        sent.state='sending'; db.commit()
    service.run_cycle()
    assert len(captured)==1
    assert any(j['state']=='uncertain' for j in admin.get(BASE).json()['jobs'])


def test_failed_notification_not_marked_sent_and_retry(clients,monkeypatch):
    author,*_,admin=clients
    data=setup_mock(admin)
    person=next(e for e in data['directory'] if e['kind']=='employee')
    admin.put(f"{BASE}/directory/{person['id']}/link",json={'user_id':1})
    t=create(author)
    admin.post(f"/api/tickets/{t['id']}/comments",json={'body':'Новый комментарий'})
    class Fail(MockClient):
        def call(self,method,params=None):
            if method=='im.notify.personal.add':return {'result':False}
            return super().call(method,params)
    monkeypatch.setattr(service,'client',lambda mode:Fail())
    service.run_cycle()
    errors=[j for j in admin.get(BASE).json()['jobs'] if j['state']=='error']
    assert len(errors)==1 and errors[0]['message']=='NOTIFICATION_NOT_DELIVERED'
    monkeypatch.setattr(service,'client',lambda mode:MockClient())
    assert admin.post(f"{BASE}/jobs/{errors[0]['id']}/retry").status_code==200
    service.run_cycle()
    assert next(j for j in admin.get(BASE).json()['jobs'] if j['id']==errors[0]['id'])['state']=='sent'


def test_unknown_task_creation_is_reconciled_never_duplicated(clients):
    author,*_,admin=clients
    data=setup_mock(admin); t=create(author)
    person=next(e for e in data['directory'] if e['kind']=='employee')
    admin.patch(f"/api/tickets/{t['id']}",json={'assignee_id':person['user_id'],'status':'IN_PROGRESS'})
    admin.put(f"{BASE}/tickets/{t['id']}",json={'enabled':True})
    class Timeout(MockClient):
        creates=0
        recovered=False
        def call(self,m,p=None):
            if m=='tasks.task.list':return {'result':{'tasks':[{'id':'777'}] if self.recovered else []}}
            if m=='tasks.task.add':
                self.creates+=1
                raise BitrixError('TIMEOUT',uncertain=True)
            if m=='tasks.task.get':return {'result':{'task':{'id':'777','status':'3','responsibleId':'9001'}}}
            return super().call(m,p)
    api=Timeout()
    with service.SessionLocal() as db:
        link=db.scalar(select(BitrixTask)); person=db.scalar(select(DirectoryEntry).where(DirectoryEntry.user_id==person['user_id']))
        link.scope=person.scope='https://example.bitrix24.kz'; db.commit()
        with pytest.raises(BitrixError):service.process_task(db,link,api)
        db.commit(); assert link.state=='uncertain'
        service.process_task(db,link,api); db.commit(); assert api.creates==1
        api.recovered=True
        service.process_task(db,link,api); db.commit()
        assert link.external_id==777 and api.creates==1


def test_remote_completion_requires_result_and_does_not_overwrite_rework(clients):
    author,*_,admin=clients
    data=setup_mock(admin); t=create(author)
    person=next(e for e in data['directory'] if e['kind']=='employee')
    admin.patch(f"/api/tickets/{t['id']}",json={'assignee_id':person['user_id'],'status':'IN_PROGRESS'})
    admin.put(f"{BASE}/tickets/{t['id']}",json={'enabled':True}); service.run_cycle()
    class Remote(MockClient):
        status='5'
        result=''
        def call(self,m,p=None):
            if m=='tasks.task.get':return {'result':{'task':{'status':self.status,'responsibleId':'9001'}}}
            if m=='tasks.task.result.list':return {'result':[{'text':self.result}]}
            return super().call(m,p)
    api=Remote()
    with service.SessionLocal() as db:
        link=db.scalar(select(BitrixTask));link.scope='https://example.bitrix24.kz';db.commit()
        service.process_task(db,link,api);db.commit()
        assert link.state=='needs_result' and db.get(Ticket,t['id']).status=='IN_PROGRESS'
        api.result='Новый исправленный результат';service.process_task(db,link,api);db.commit()
    assert author.post(f"/api/tickets/{t['id']}/resolution-review",json={'decision':'rework','expected_version':1,'explanation':'Проблема сохраняется'}).status_code==200
    with service.SessionLocal() as db:
        link=db.scalar(select(BitrixTask));service.process_task(db,link,api);db.commit()
        assert db.get(Ticket,t['id']).status=='IN_PROGRESS'
        api.status='3';service.process_task(db,link,api);db.commit()
        api.status='5';api.result='Повторно исправлено';service.process_task(db,link,api);db.commit()
        assert db.get(Ticket,t['id']).resolution_review_version==2


def test_directory_failed_snapshot_does_not_disable_accounts(clients):
    *_,admin=clients
    setup_mock(admin)
    class Broken(MockClient):
        def all(self,m):
            if m=='user.get':raise BitrixError('NETWORK')
            return super().all(m)
    with service.SessionLocal() as db:
        ids=list(db.scalars(select(DirectoryEntry.user_id).where(DirectoryEntry.kind=='employee')))
        with pytest.raises(BitrixError):service.directory_sync(db,Broken(),'mock')
        db.rollback()
        assert all(db.get(User,id).is_active for id in ids)


def test_pagination_and_webhook_error_redaction(monkeypatch):
    monkeypatch.setenv('BITRIX_WEBHOOK_URL','https://example.bitrix24.kz/rest/1/not-a-real-secret/')
    api=BitrixClient();calls=[]
    def call(method,p):
        calls.append(p['start'])
        return {'result':[{'ID':1}], 'next':50} if p['start']==0 else {'result':[{'ID':2}]}
    monkeypatch.setattr(api,'call',call)
    assert len(api.all('user.get'))==2 and calls==[0,50]


def test_public_links_use_published_site(monkeypatch):
    monkeypatch.delenv('MODUS_PUBLIC_URL', raising=False)
    assert service.public_url() == 'https://modus-trade.makquiz.site'
    monkeypatch.setenv('MODUS_PUBLIC_URL', 'https://feedback.example.kz/')
    assert service.public_url() == 'https://feedback.example.kz'
    for invalid in ('http://localhost:5173', 'http://127.0.0.1:5173', 'http://[::1]:5173',
                    'http://192.168.1.10', 'http://host.local', 'https://example.kz/?token=x'):
        monkeypatch.setenv('MODUS_PUBLIC_URL', invalid)
        with pytest.raises(BitrixError, match='INVALID_PUBLIC_URL'):
            service.public_url()
