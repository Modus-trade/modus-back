from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from ..security import Admin, DB
from ..models import now
from ..integration_models import BitrixBot, BotReceipt
from ..services.bitrix_client import client, BitrixError
from ..services import bitrix_bot as service
from .integrations import realm_for, ensure_idle, settings

router = APIRouter(prefix='/api/integrations/bitrix/bot', tags=['bitrix chatbot'])


@router.get('')
def status(db: DB, user: Admin):
    realm = realm_for(db)
    bot = db.scalar(select(BitrixBot).where(BitrixBot.scope == realm))
    if not bot: return {'enabled':False,'health':'Не подключён','external_id':None,'events':[]}
    return {'enabled':bot.enabled,'health':bot.health,'external_id':bot.external_id,'checked_at':bot.checked_at,
        'events':[{'event_id':r.event_id,'state':r.state,'error':r.error,'created_at':r.created_at}
            for r in db.scalars(select(BotReceipt).where(BotReceipt.bot_id==bot.id).order_by(BotReceipt.id.desc()).limit(20))]}


@router.post('/register')
def register(db: DB, user: Admin):
    cfg = settings(db)
    ensure_idle(cfg)
    if cfg.mode=='off':raise HTTPException(422,'Включите интеграцию Битрикс24')
    try:
        bot=service.register(db,realm_for(db),client(cfg.mode))
        return {'external_id':bot.external_id,'enabled':bot.enabled}
    except BitrixError as e:
        raise HTTPException(422,e.code)


class Enable(BaseModel):
    enabled: bool


@router.put('')
def enable(data:Enable,db:DB,user:Admin):
    ensure_idle(settings(db))
    bot=db.scalar(select(BitrixBot).where(BitrixBot.scope==realm_for(db)))
    if not bot or not bot.external_id:raise HTTPException(422,'Сначала подключите бота')
    bot.enabled=data.enabled
    db.commit()
    return {'enabled':bot.enabled}


class MockMessage(BaseModel):
    external_user_id:int=Field(gt=0)
    text:str=Field(min_length=1,max_length=20000)


@router.post('/mock-message')
def mock_message(data:MockMessage,db:DB,user:Admin):
    cfg=settings(db)
    ensure_idle(cfg)
    if cfg.mode!='mock':raise HTTPException(422,'Доступно только в МОК-режиме')
    bot=db.scalar(select(BitrixBot).where(BitrixBot.scope=='mock'))
    if not bot or not bot.enabled:raise HTTPException(422,'Подключите бота')
    # The admin-only simulator runs the exact same state machine as authenticated portal events.
    from sqlalchemy import func
    event_id=(db.scalar(select(func.max(BotReceipt.event_id)).where(BotReceipt.bot_id==bot.id)) or 0)+1
    service.process_event(db,bot,{'eventId':event_id,'type':'ONIMBOTV2MESSAGEADD','data':{
        'chat':{'type':'private','dialogId':str(data.external_user_id)},
        'user':{'id':data.external_user_id,'bot':False,'active':True},
        'message':{'authorId':data.external_user_id,'text':data.text}}},client('mock'))
    db.flush()
    receipt=db.scalar(select(BotReceipt).where(BotReceipt.bot_id==bot.id,BotReceipt.event_id==event_id))
    reply=receipt.reply
    receipt.state='sent'
    receipt.reply=''
    db.commit()
    return {'reply':reply}
