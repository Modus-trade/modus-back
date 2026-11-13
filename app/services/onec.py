"""Demonstration catalog. No connection to a live 1C database."""
from uuid import uuid5, NAMESPACE_URL

ITEMS = [dict(Ref_Key=str(uuid5(NAMESPACE_URL, 'modus:onec:demo:'+code)), Description=name,
              Code=code, Артикул=article, DeletionMark=False)
         for code,name,article in [
             ('УТ-000123','Кабель КСПВ 4×0,5','КСПВ-4-05'),
             ('УТ-000124','Извещатель дымовой ИП 212-141','ИП-212-141'),
             ('УТ-000125','Прибор приёмно-контрольный С2000-М','С2000-М'),
             ('УТ-000126','Кабель UTP Cat.6','UTP-CAT6'),
             ('УТ-000127','Источник питания 12 В, 3 А','БП-12-3'),
             ('УТ-000128','Камера IP купольная 4 Мп','IPC-4MP')]]

def find_item(ref):
    return next((item for item in ITEMS if item['Ref_Key'] == str(ref)), None)

# Explicit mock orders and suppliers; never returned as real business data.
import os
import json
import hashlib
from datetime import datetime
from sqlalchemy import select
from ..models import now
from ..onec_models import OneCSettings, OneCRecord, OneCExchange
from .onec_client import OneCError, ODataClient, base_url, field

def guid(value): return str(uuid5(NAMESPACE_URL, 'modus:onec:demo:'+value))
PARTNERS = [{'Ref_Key':guid('supplier1'),'Description':'ТОО КабельСнаб · Демо','Code':'П-001'},
            {'Ref_Key':guid('supplier2'),'Description':'ТОО Безопасность Плюс · Демо','Code':'П-002'},
            {'Ref_Key':guid('customer1'),'Description':'Бизнес-центр Север · Демо','Code':'К-001'}]
DOCUMENTS = [dict(Ref_Key=guid(code), Number=code, Date=date, kind=kind,
                  partner=PARTNERS[partner], Description=('Заказ поставщику' if kind=='purchase' else 'Заказ клиента'))
             for code,date,kind,partner in [('ЗП-000101','2026-11-01T09:00:00','purchase',0),
                 ('ЗП-000102','2026-11-04T10:00:00','purchase',1),
                 ('ЗК-000201','2026-11-06T11:00:00','sale',2)]]

def settings(db):
    cfg=db.get(OneCSettings,1)
    if not cfg:
        cfg=OneCSettings(id=1,mode='mock',health='Демонстрационный каталог')
        db.add(cfg); db.flush()
    return cfg

def scope(mode):
    return 'mock' if mode=='mock' else 'real:'+hashlib.sha256(base_url().encode()).hexdigest()[:32]

def log(db, mode, operation, state, message):
    db.add(OneCExchange(mode=mode,operation=operation,state=state,message=message))

def allow_documents(user, mode):
    return mode=='mock' or user.role in ('ADMIN','MANAGER') or os.getenv('ONEC_ALLOW_EMPLOYEE_DOCUMENTS','false').lower()=='true'

def records(db,kind):
    mode=settings(db).mode
    if mode=='off': return []
    if mode=='mock':
        return ITEMS if kind=='items' else PARTNERS if kind=='partners' else [d for d in DOCUMENTS if d['kind']==kind]
    return [json.loads(r.payload) for r in db.scalars(select(OneCRecord).where(
        OneCRecord.scope==scope(mode),OneCRecord.kind==kind,OneCRecord.active.is_(True)))]

def snapshot(db, kind, ref, expected_scope=None):
    mode=settings(db).mode
    if mode=='off': raise OneCError('Обмен 1С выключен. Уберите привязку или включите интеграцию')
    realm=scope(mode)
    if expected_scope and realm!=expected_scope: raise OneCError('Источник 1С изменился. Выберите позицию заново')
    row=next((r for r in records(db,kind) if r['Ref_Key']==str(ref)),None)
    if not row: raise OneCError('Запись 1С не найдена или больше недоступна. Выберите другую')
    return {**row,'mode':mode,'scope':realm,'linked_at':now().isoformat()}

def sync(db):
    cfg=settings(db)
    if cfg.mode=='off': raise OneCError('Обмен выключен')
    realm=scope(cfg.mode)
    if cfg.mode=='mock':
        counts={'items':len(ITEMS),'partners':len(PARTNERS),'purchase':2,'sale':1}
    else:
        api=ODataClient()
        raw={kind:api.all(kind) for kind in ('items','partners','purchase','sale')}
        partners={r['Ref_Key']:{k:r.get(k,'') for k in ('Ref_Key','Description','Code')} for r in raw['partners']}
        normalized={'partners':list(partners.values()),'items':[], 'purchase':[], 'sale':[]}
        for r in raw['items']:
            normalized['items'].append({k:r.get(k,'') for k in ('Ref_Key','Description','Code')} | {'Артикул':r.get(field('ARTICLE','Артикул'),'')})
        for kind in ('purchase','sale'):
            for r in raw[kind]:
                normalized[kind].append({k:r.get(k,'') for k in ('Ref_Key','Number','Date')} | {
                    'kind':kind,'Description':'Заказ поставщику' if kind=='purchase' else 'Заказ клиента',
                    'partner':partners.get(r.get(field('PARTNER','Контрагент_Key')))})
        # Only replace active records after all four collections have been read successfully.
        old={(r.kind,r.ref):r for r in db.scalars(select(OneCRecord).where(OneCRecord.scope==realm))}
        for row in old.values(): row.active=False
        for kind,rows in normalized.items():
            for data in rows:
                row=old.get((kind,data['Ref_Key']))
                if row is None:
                    row=OneCRecord(scope=realm,kind=kind,ref=data['Ref_Key']); db.add(row)
                row.active=True; row.payload=json.dumps(data,ensure_ascii=False)
        counts={kind:len(rows) for kind,rows in normalized.items()}
    cfg.synced_at=now(); cfg.health='Каталог обновлён'
    log(db,cfg.mode,'sync','success','Обновлено: '+', '.join(f'{k}: {v}' for k,v in counts.items()))
    db.commit()
    return counts
