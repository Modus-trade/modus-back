import json
import pytest
from sqlalchemy import select, func
from test_workflow import clients  # noqa: F401
from app.models import Ticket, User, Category, TicketParticipant, Attachment, now
from app.integration_models import BitrixBot, BotDialog, BotReceipt, DirectoryEntry
from app.services import integrations, bitrix_bot as bot
from app.services.bitrix_client import MockClient, BitrixError

BASE='/api/integrations/bitrix/bot'


def setup(admin):
    assert admin.put('/api/integrations/bitrix/mode',json={'mode':'mock'}).status_code==200
    admin.post('/api/integrations/bitrix/sync');integrations.run_cycle()
    assert admin.post(BASE+'/register').status_code==200


def send(admin,text,person=9001):
    r=admin.post(BASE+'/mock-message',json={'external_user_id':person,'text':text})
    assert r.status_code==200,r.text
    return r.json()['reply']


def event(id,text,user=9001,**extra):
    data={'user':{'id':user,'bot':False,'active':True},'chat':{'type':'private','dialogId':str(user)},
          'message':{'authorId':user,'text':text}}
    data.update(extra)
    return {'eventId':id,'type':'ONIMBOTV2MESSAGEADD','data':data}


def test_intake_confirmation_source_and_no_duplicates(clients):
    *_,admin=clients
    setup(admin)
    assert 'Напишите' in send(admin,'начать')
    assert 'категор' in send(admin,'Принтер в офисе не печатает документы')
    assert 'Проверьте' in send(admin,'1')
    assert admin.get('/api/tickets').json()['total']==0
    reply=send(admin,'отправить')
    assert 'A-001' in reply
    assert 'Предыдущее действие' in send(admin,'отправить')
    ticket=admin.get('/api/tickets').json()['items'][0]
    assert ticket['source']=='BITRIX24' and not ticket['is_shared']
    assert ticket['description']=='Принтер в офисе не печатает документы'
    with integrations.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Ticket))==1
        receipt=db.scalar(select(BotReceipt).order_by(BotReceipt.id.desc()))
        bot.process_event(db,db.scalar(select(BitrixBot)),event(receipt.event_id,'отправить'),MockClient())
        db.commit()
        assert db.scalar(select(func.count()).select_from(Ticket))==1


def test_permissions_unknown_inactive_group_and_author_spoofing(clients):
    author,*_,admin=clients
    assert author.post(BASE+'/register').status_code==403
    setup(admin)
    assert 'не связан' in send(admin,'Что-то сломалось',123456)
    with integrations.SessionLocal() as db:
        b=db.scalar(select(BitrixBot))
        bot.process_event(db,b,event(100,'описание проблемы',chat={'type':'chat','dialogId':'chat42'}),MockClient())
        bot.process_event(db,b,event(101,'описание проблемы',message={'authorId':9002,'text':'Текст чужого пользователя'}),MockClient())
        bot.process_event(db,b,event(102,'описание проблемы',user=9002,bot={'id':999999}),MockClient())
        db.commit()
        assert db.scalar(select(func.count()).select_from(BotDialog))==0
        row=db.scalar(select(DirectoryEntry).where(DirectoryEntry.external_id==9001))
        db.get(User,row.user_id).is_active=False;db.commit()
    assert 'не связан' in send(admin,'Что-то сломалось')
    assert admin.get('/api/tickets').json()['total']==0


def test_similar_only_shared_and_join_confirmation(clients):
    *_,admin=clients
    setup(admin)
    with integrations.SessionLocal() as db:
        for i,shared in [(1,False),(2,True)]:
            db.add(Ticket(id=i,number=f'A-{i:03d}',type='PROBLEM',title='Принтер в офисе не печатает документы',
                description='PRIVATE DESCRIPTION MUST NOT LEAK',category_id=1,author_id=1,is_shared=shared,due_at=now()))
        db.commit()
    send(admin,'Принтер в офисе не печатает документы')
    reply=send(admin,'1')
    assert 'A-002' in reply and 'A-001' not in reply and 'PRIVATE' not in reply
    assert 'Выберите номер' in send(admin,'присоединиться A-001')
    assert 'подтвердить' in send(admin,'присоединиться A-002')
    with integrations.SessionLocal() as db:assert db.scalar(select(func.count()).select_from(TicketParticipant))==0
    assert 'присоединились' in send(admin,'подтвердить')
    send(admin,'подтвердить')
    with integrations.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(TicketParticipant))==1
        assert db.scalar(select(func.count()).select_from(Ticket))==2


def test_cancel_change_category_type_and_expiration(clients):
    *_,admin=clients
    setup(admin)
    assert 'Напишите' in send(admin,'категория 1')
    send(admin,'Первое описание проблемы с принтером')
    send(admin,'1')
    send(admin,'тип предложение')
    send(admin,'изменить')
    send(admin,'Предлагаю сделать обучение для коллег')
    send(admin,'2')
    assert 'Черновик отменён' in send(admin,'отмена')
    send(admin,'отправить')
    assert admin.get('/api/tickets').json()['total']==0
    send(admin,'Принтер опять не работает и не печатает')
    send(admin,'1')
    from datetime import timedelta
    with integrations.SessionLocal() as db:
        db.scalar(select(BotDialog)).updated_at=now()-timedelta(days=2);db.commit()
    send(admin,'отправить')
    assert admin.get('/api/tickets').json()['total']==0


def test_rollback_and_restart_preserve_confirmation(clients):
    *_,admin=clients
    setup(admin)
    send(admin,'Нет доступа к системе учета документов')
    send(admin,'1')
    with integrations.SessionLocal() as db:
        b=db.scalar(select(BitrixBot))
        bot.process_event(db,b,event(50,'отправить'),MockClient())
        db.rollback()
    assert admin.get('/api/tickets').json()['total']==0
    with integrations.SessionLocal() as db:
        b=db.scalar(select(BitrixBot))
        bot.process_event(db,b,event(50,'отправить'),MockClient());db.commit()
    assert admin.get('/api/tickets').json()['total']==1


def test_files_and_failure_require_explicit_resolution(clients,monkeypatch,tmp_path):
    *_,admin=clients
    setup(admin)
    send(admin,'Нет доступа к системе учета документов');send(admin,'1')
    def fail(*args):raise BitrixError('BOT_FILE_TOO_LARGE')
    monkeypatch.setattr(bot,'fetch_file',fail)
    with integrations.SessionLocal() as db:
        b=db.scalar(select(BitrixBot))
        bot.process_event(db,b,event(40,'',message={'authorId':9001,'text':'','params':{'FILE_ID':['15']}}),MockClient());db.commit()
    assert 'Сначала повторите' in send(admin,'отправить')
    assert admin.get('/api/tickets').json()['total']==0
    def success(*args):
        f=tmp_path/'document.txt';f.write_text('test')
        return {'external_id':15,'filename':'document.txt','stored_path':str(f),'content_type':'text/plain','size_bytes':4}
    monkeypatch.setattr(bot,'fetch_file',success)
    with integrations.SessionLocal() as db:
        bot.process_event(db,db.scalar(select(BitrixBot)),event(100,'',message={'authorId':9001,'text':'','params':{'FILE_ID':['15']}}),MockClient());db.commit()
    send(admin,'отправить')
    with integrations.SessionLocal() as db:
        a=db.scalar(select(Attachment));assert a.filename=='document.txt' and a.ticket_id==1


def test_poll_cursor_and_unknown_reply_are_safe(clients,monkeypatch):
    *_,admin=clients
    setup(admin)
    monkeypatch.setenv('BITRIX_BOT_TOKEN','test-token-at-least-16-characters')
    class Fetch:
        calls=[]
        def call(self,method,p):
            self.calls.append((method,p))
            if method.endswith('Event.get'):return {'result':{'events':[event(10,'начать')],'nextOffset':11}}
            raise BitrixError('TIMEOUT',uncertain=True)
    api=Fetch()
    with integrations.SessionLocal() as db:
        b=db.scalar(select(BitrixBot));bot.poll(db,b,api)
        assert b.cursor==11
        assert db.scalar(select(BotReceipt)).state=='uncertain'
        bot.poll(db,b,api)
        assert len([m for m,p in api.calls if m.endswith('Message.send')])==1
        assert api.calls[-1][1]['offset']==11


def test_file_download_rejects_arbitrary_hosts(clients,monkeypatch):
    *_,admin=clients
    setup(admin)
    monkeypatch.setenv('BITRIX_BOT_TOKEN','test-token-at-least-16-characters')
    class Download:
        def call(self,*args):return {'result':{'downloadUrl':'http://127.0.0.1/private'}}
    with integrations.SessionLocal() as db:
        b=db.scalar(select(BitrixBot));b.scope='https://example.bitrix24.kz'
        with pytest.raises(BitrixError,match='BOT_FILE_ORIGIN_REJECTED'):bot.fetch_file(Download(),b,10)


def test_mapping_change_drops_previous_draft(clients):
    *_,admin=clients
    setup(admin)
    send(admin,'Описание проблемы предыдущего пользователя');send(admin,'1')
    with integrations.SessionLocal() as db:
        row=db.scalar(select(DirectoryEntry).where(DirectoryEntry.external_id==9001));row.user_id=2;db.commit()
    assert 'Напишите' in send(admin,'отправить')
    assert admin.get('/api/tickets').json()['total']==0


def test_expired_upload_cleanup_preserves_active_draft(clients, tmp_path, monkeypatch):
    import os
    import time
    from datetime import timedelta
    *_, admin = clients
    setup(admin)
    send(admin, 'начать')
    monkeypatch.setattr(bot, 'UPLOADS', tmp_path)
    expired = tmp_path / 'bot_expired.txt'
    active = tmp_path / 'bot_active.txt'
    for path in (expired, active):
        path.write_text('test')
        os.utime(path, (time.time() - 90000, time.time() - 90000))
    with integrations.SessionLocal() as db:
        dialog = db.scalar(select(BotDialog))
        dialog.draft = json.dumps({'files': [{'stored_path': str(active)}]})
        dialog.updated_at = now()
        db.commit()
        bot.cleanup_drafts(db)
        assert not expired.exists() and active.exists()
        dialog.updated_at = now() - timedelta(hours=25)
        db.commit()
        bot.cleanup_drafts(db)
        db.commit()
        assert dialog.draft == '{}' and not active.exists()
