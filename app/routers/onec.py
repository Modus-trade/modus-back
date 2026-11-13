import json
from typing import Literal
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from ..security import CurrentUser, Admin, DB
from ..onec_models import OneCExchange
from ..services import onec as service
from ..services.onec_client import OneCError, ODataClient, collection
from ..services.tickets import visible, require_manager
from ..models import Ticket

router=APIRouter(prefix='/api/integrations/onec', tags=['1C'])

@router.get('')
def status(db:DB,user:Admin):
    cfg=service.settings(db)
    try:
        counts={kind:len(service.records(db,kind)) for kind in ('items','purchase','sale')}
        configured=bool(ODataClient())
    except OneCError:
        counts={kind:len(service.records(db,kind)) if cfg.mode!='real' else 0 for kind in ('items','purchase','sale')}
        configured=False
    return {'mode':cfg.mode,'health':cfg.health,'synced_at':cfg.synced_at,'configured':configured,
        'counts':counts,'jobs':db.scalars(select(OneCExchange).order_by(OneCExchange.id.desc()).limit(30)).all()}

class Mode(BaseModel):
    mode:Literal['mock','real','off']

@router.put('/mode')
def mode(data:Mode,db:DB,user:Admin):
    if data.mode=='real':
        try: ODataClient()
        except OneCError as e: raise HTTPException(422,str(e))
    cfg=service.settings(db)
    cfg.mode=data.mode; cfg.synced_at=None; cfg.health='Режим изменён; обновите каталог' if data.mode!='off' else 'Обмен выключен'
    service.log(db,data.mode,'mode','success','Режим: '+data.mode)
    db.commit()
    return {'mode':data.mode}

@router.post('/sync')
def sync(db:DB,user:Admin):
    mode=service.settings(db).mode
    try: return service.sync(db)
    except OneCError as e:
        db.rollback()
        cfg=service.settings(db); cfg.health=str(e)
        service.log(db,mode,'sync','error',str(e)); db.commit()
        raise HTTPException(502,str(e))

@router.post('/check')
def check(db:DB,user:Admin):
    cfg=service.settings(db)
    try:
        if cfg.mode=='off': raise OneCError('Обмен выключен')
        if cfg.mode=='real':
            api=ODataClient()
            for kind in ('items','partners','purchase','sale'): api.all(kind)
        cfg.health='Чтение коллекций доступно' if cfg.mode=='real' else 'Демонстрационный источник доступен'
        service.log(db,cfg.mode,'check','success',cfg.health); db.commit()
        return {'message':cfg.health}
    except OneCError as e:
        service.log(db,cfg.mode,'check','error',str(e)); cfg.health=str(e); db.commit()
        raise HTTPException(502,str(e))

@router.get('/items')
def items(db:DB,user:CurrentUser,q:str=Query('',max_length=200)):
    return search(db,user,'items',q)

@router.get('/documents')
def documents(db:DB,user:CurrentUser,kind:Literal['purchase','sale']='purchase',q:str=Query('',max_length=200)):
    return search(db,user,kind,q)

def search(db,user,kind,q):
    cfg=service.settings(db)
    if kind!='items' and not service.allow_documents(user,cfg.mode):
        raise HTTPException(403,'Доступ сотрудников к документам 1С не включён администратором')
    try:
        rows=service.records(db,kind)
        rows=[r for r in rows if all(t in json.dumps(r,ensure_ascii=False).casefold() for t in q.casefold().split())]
        return {'mode':cfg.mode,'scope':service.scope(cfg.mode) if cfg.mode!='off' else 'off',
                'synced_at':cfg.synced_at,'total':len(rows),'value':rows[:100]}
    except OneCError as e: raise HTTPException(503,str(e))

@router.get('/analytics')
def analytics(db:DB,user:CurrentUser,mode:Literal['mock','real']='mock',days:int=Query(90,ge=1,le=3650),
              group:Literal['items','suppliers']='items',key:str|None=None,page:int=Query(1,ge=1)):
    from datetime import timedelta
    from ..models import now
    from ..services.problems import affected_count
    require_manager(user)
    query=visible(user).where(Ticket.type=='PROBLEM',Ticket.created_at>=now()-timedelta(days=days))
    rows=db.execute(query.add_columns(affected_count())).all()
    groups={}; matching=[]
    for ticket,affected in rows:
        item=json.loads(ticket.onec_item) if ticket.onec_item else None
        doc=json.loads(ticket.onec_document) if ticket.onec_document else None
        data=item if group=='items' else doc
        if not data or data.get('mode')!=mode: continue
        if group=='suppliers':
            if doc.get('kind')!='purchase' or not doc.get('partner'): continue
            entity=doc['partner']
        else: entity=item
        identity=data.get('scope',data['mode'])+':'+entity['Ref_Key']
        row=groups.setdefault(identity,{'key':identity,'name':entity['Description'],'code':entity.get('Code',''),
            'count':0,'open':0,'participations':0})
        row['count']+=1; row['open']+=int(ticket.status not in ('CLOSED','REJECTED')); row['participations']+=affected
        if identity==key: matching.append({'id':ticket.id,'number':ticket.number,'title':ticket.title,'status':ticket.status})
    ordered=sorted(groups.values(),key=lambda r:(-r['count'],-r['open'],r['name']))
    return {'mode':mode,'groups':ordered,'total':len(matching),'tickets':matching[(page-1)*20:page*20]}
