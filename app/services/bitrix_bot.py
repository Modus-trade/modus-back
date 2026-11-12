"""Private-chat intake. Durable dialogs, transactional receipts and explicit confirmation.

No public event endpoint: events are read with the authenticated imbot.v2 fetch API.
Untrusted event fields never choose a local user or a download endpoint.
"""
import json
import os
import re
import uuid
from datetime import timedelta
from email.message import Message
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from ..database import BASE_DIR
from ..models import Attachment, Category, Ticket, TicketParticipant, User, now
from ..integration_models import BitrixBot, BotDialog, BotReceipt, DirectoryEntry
from .bitrix_client import BitrixError
from .integrations import active, public_url, settings
from .bot_matching import ACTIVE, affected_count, similarity
from .hints import rule_category, rule_type
from .tickets import eligible_assignee_ids
from .notifications import notify
from .live import ticket_changed

UPLOADS = BASE_DIR / 'uploads'
MAX_BYTES = 10 * 1024 * 1024
TYPES = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.txt': 'text/plain'}
TYPE_NAMES = {'PROBLEM': 'Проблема', 'PROPOSAL': 'Предложение', 'PRAISE': 'Благодарность'}
HELP = ('Я помогу подать обращение в Модус. Напишите, что случилось или что предлагаете улучшить (от 10 символов). '
        'Можно приложить до 5 файлов: PDF, PNG, JPG, DOCX, XLSX, TXT, до 10 МБ каждый.\n'
        'Команды: «новое», «статус», «изменить», «отмена». Отправка — только после вашего подтверждения.')


def token():
    value = os.getenv('BITRIX_BOT_TOKEN', '')
    if not 16 <= len(value) <= 40: raise BitrixError('BOT_TOKEN_NOT_CONFIGURED')
    return value


def credentials(bot):
    return {'botId': bot.external_id, 'botToken': token()}


def plain(text):
    # Avoid user-supplied BBCode producing links/mentions in bot previews.
    return str(text).replace('[', '［').replace(']', '］')


def register(db, realm, api):
    bot = db.scalar(select(BitrixBot).where(BitrixBot.scope == realm))
    if not bot:
        bot = BitrixBot(scope=realm)
        db.add(bot)
        db.flush()
    code = 'modus_feedback_' + settings(db).instance_id
    db.commit()  # Persist identity before network call; identical code/token registration is idempotent.
    if realm == 'mock':
        bot.external_id = 99001
    else:
        result = api.call('imbot.v2.Bot.register', {'fields': {'code': code, 'botToken': token(),
            'type': 'bot', 'eventMode': 'fetch', 'isHidden': False,
            'properties': {'name': 'Модус · Обращения', 'workPosition': 'Проблемы и предложения сотрудников'}}})['result']
        bot.external_id = int(result['bot']['id'])
    bot.enabled, bot.health = True, 'Подключён. Напишите боту в личном чате.'
    db.commit()
    return bot


def mapped_user(db, realm, external_id):
    entry = db.scalar(select(DirectoryEntry).where(DirectoryEntry.scope == realm,
        DirectoryEntry.kind == 'employee', DirectoryEntry.external_id == external_id))
    if not entry or not json.loads(entry.payload).get('active'): return None
    user = db.get(User, entry.user_id) if entry.user_id else None
    return user if user and user.is_active else None


def categories(db):
    return db.scalars(select(Category).where(Category.is_active).order_by(Category.sort_order, Category.id)).all()


def category_prompt(db, draft):
    cat = db.get(Category, draft.get('category_id')) if draft.get('category_id') else None
    recommended = f'Предлагаю категорию «{plain(cat.name)}». Напишите «да», если подходит.\n' if cat and cat.is_active else ''
    return (f'Тип: {TYPE_NAMES[draft["type"]]}.\n{recommended}Или выберите ID категории:\n' +
        '\n'.join(f'{c.id} — {plain(c.name)}' for c in categories(db)) +
        '\nТип можно изменить командой «тип проблема», «тип предложение» или «тип благодарность».')


def candidates(db, draft):
    if draft.get('type') != 'PROBLEM': return []
    ranked = [(similarity(draft['description'], t.title), t) for t in db.scalars(select(Ticket).where(
        Ticket.is_shared.is_(True), Ticket.type == 'PROBLEM', Ticket.status.in_(ACTIVE)))]
    return [t for score, t in sorted(ranked, key=lambda v: (-v[0], v[1].id))[:3] if score >= .38]


def preview(db, draft):
    cat = db.get(Category, draft.get('category_id')) if draft.get('category_id') else None
    if not cat or not cat.is_active:
        draft['step'] = 'category'
        return 'Категория больше недоступна.\n' + category_prompt(db, draft)
    draft['step'] = 'review'
    similar = candidates(db, draft)
    draft['similar_ids'] = [t.id for t in similar]
    result = (f'Проверьте обращение:\nТема: {plain(draft["title"])}\nТип: {TYPE_NAMES[draft["type"]]}\n'
        f'Категория: {plain(cat.name)}\nОписание: {plain(draft["description"][:1500])}' +
        ('… (полный текст сохранится)' if len(draft['description']) > 1500 else '') +
        '\nФайлы: ' + (', '.join(plain(f['filename']) for f in draft.get('files', [])) or 'нет'))
    if similar:
        result += '\n\nЕсть похожие общие проблемы:'
        for t in similar:
            count = db.scalar(select(affected_count()).where(Ticket.id == t.id))
            result += f'\n{t.number}: {plain(t.title)} — столкнулись {count} чел.'
        result += '\nЧтобы присоединиться, напишите «присоединиться A-номер».'
    return result + '\n\n«Отправить» — создать новое обращение. «Изменить» — переписать описание. «Категория ID» — изменить категорию. «Отмена» — удалить черновик. Обращение будет приватным.'


def fetch_file(api, bot, file_id):
    result = api.call('imbot.v2.File.download', {**credentials(bot), 'fileId': int(file_id)})['result']
    url = result.get('downloadUrl', '')
    parsed, origin = urlsplit(url), urlsplit(bot.scope)
    if parsed.scheme != 'https' or parsed.netloc != origin.netloc or parsed.username:
        raise BitrixError('BOT_FILE_ORIGIN_REJECTED')
    # Follow no redirects: never send a credential-bearing URL to an arbitrary host.
    try:
        with httpx.stream('GET', url, timeout=20, follow_redirects=False) as response:
            if response.status_code != 200: raise BitrixError('BOT_FILE_DOWNLOAD_FAILED')
            disposition = Message()
            disposition['content-disposition'] = response.headers.get('content-disposition', '')
            filename = Path((disposition.get_filename() or '').replace('\\', '/')).name
            content_type = response.headers.get('content-type', '').split(';')[0].lower()
            if not filename:
                ext = next((ext for ext, mime in TYPES.items() if mime == content_type), '')
                filename = f'Файл-{file_id}{ext}'
            ext = Path(filename).suffix.lower()
            if ext not in TYPES: raise BitrixError('BOT_FILE_TYPE_NOT_ALLOWED')
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES: raise BitrixError('BOT_FILE_TOO_LARGE')
    except httpx.HTTPError:
        raise BitrixError('BOT_FILE_DOWNLOAD_FAILED') from None
    if not content: raise BitrixError('BOT_FILE_EMPTY')
    UPLOADS.mkdir(exist_ok=True)
    path = UPLOADS / f'bot_{uuid.uuid4().hex}{ext}'
    path.write_bytes(content)
    return {'external_id': int(file_id), 'filename': filename[:255], 'stored_path': str(path),
            'content_type': TYPES[ext], 'size_bytes': len(content)}


def create_from_draft(db, user, draft):
    category = db.get(Category, draft['category_id'])
    if not category or not category.is_active: raise BitrixError('BOT_CATEGORY_INACTIVE')
    assignee_id = category.default_assignee_id if category.default_assignee_id in eligible_assignee_ids(db) else None
    # Auto-increment is atomic and shares the same SQLite sequence/Postgres sequence as web tickets.
    ticket = Ticket(number='BOT-' + uuid.uuid4().hex[:24], source='BITRIX24', type=draft['type'],
        category_id=category.id, title=draft['title'], description=draft['description'],
        author_id=user.id, assignee_id=assignee_id, is_shared=False,
        due_at=now() + timedelta(hours=category.sla_first_response_hours))
    db.add(ticket)
    db.flush()
    ticket.number = f'A-{ticket.id:03d}'
    for f in draft.get('files', []):
        if not Path(f['stored_path']).is_file(): raise BitrixError('BOT_FILE_MISSING')
        db.add(Attachment(ticket_id=ticket.id, uploaded_by_id=user.id,
            **{key: f[key] for key in ('filename', 'stored_path', 'content_type', 'size_bytes')}))
    notify(db, ticket, user.id, 'assigned' if assignee_id else 'created')
    ticket_changed(db, ticket, 'created')
    return ticket


def respond(db, bot, user, dialog, text, file_ids, api):
    draft = json.loads(dialog.draft)
    if dialog.updated_at < now() - timedelta(hours=24) or dialog.user_id != user.id:
        draft = {}
        dialog.user_id = user.id
    command = text.strip().casefold()
    if command in ('/start', 'старт', 'начать', 'новое', '/new', 'отмена', '/cancel'):
        draft = {'step': 'description', 'files': []}
        return draft, ('Черновик отменён.\n' if command in ('отмена', '/cancel') else '') + HELP
    if command in ('помощь', '/help'): return draft, HELP
    if draft.get('step') == 'done':
        return draft, f'Предыдущее действие выполнено: {draft["receipt"]}\nЧтобы подать ещё одно обращение, напишите «новое».'
    if file_ids:
        existing = {f['external_id'] for f in draft.get('files', [])}
        new_ids = [i for i in dict.fromkeys(file_ids) if i not in existing]
        if len(existing) + len(new_ids) > 5: return draft, 'Можно приложить максимум 5 файлов. Напишите «без файлов», чтобы очистить вложения.'
        for file_id in new_ids:
            try: draft.setdefault('files', []).append(fetch_file(api, bot, file_id))
            except BitrixError as e:
                draft['file_error'] = True
                return draft, f'Не удалось принять файл ({e.code}). Обращение не отправлено. Пришлите файл повторно или напишите «без файлов».'
        draft.pop('file_error', None)
        if not text.strip():
            return draft, preview(db, draft) if draft.get('description') and draft.get('step') != 'category' else 'Файлы добавлены. ' + (category_prompt(db, draft) if draft.get('description') else HELP)
    if command == 'без файлов':
        draft['files'] = []
        draft.pop('file_error', None)
        return draft, preview(db, draft) if draft.get('description') else HELP
    if command == 'изменить':
        draft['step'] = 'description'
        return draft, 'Напишите новое описание целиком. Вложения сохранятся; убрать их можно командой «без файлов».'
    if command in ('статус', '/status'):
        if draft.get('description'): return draft, preview(db, draft)
        return draft, HELP
    if command.startswith('тип ') and draft.get('description'):
        choices = {'проблема':'PROBLEM', 'предложение':'PROPOSAL', 'благодарность':'PRAISE'}
        if command[4:] not in choices: return draft, 'Выберите: «тип проблема», «тип предложение», «тип благодарность».'
        draft['type'] = choices[command[4:]]
        return draft, preview(db, draft)
    if command.startswith('категория ') and not draft.get('description'): return draft, HELP
    if command.startswith('категория ') or draft.get('step') == 'category':
        value = command.removeprefix('категория ').strip()
        cat_id = draft.get('category_id') if value in ('да', 'подходит') else int(value) if value.isdigit() else None
        cat = db.get(Category, cat_id) if cat_id else None
        if not cat or not cat.is_active: return draft, category_prompt(db, draft)
        draft['category_id'] = cat.id
        return draft, preview(db, draft)
    if draft.get('step') == 'join_confirm' and command in ('да', 'подтвердить'):
        ticket = db.scalar(select(Ticket).where(Ticket.id == draft['join_id'], Ticket.is_shared.is_(True),
            Ticket.type == 'PROBLEM', Ticket.status.in_(ACTIVE)).with_for_update())
        if not ticket: return draft, 'Эта проблема уже недоступна для присоединения. Напишите «статус», чтобы выбрать другое действие.'
        if ticket.author_id != user.id:
            insert = pg_insert if db.get_bind().dialect.name == 'postgresql' else sqlite_insert
            db.execute(insert(TicketParticipant).values(ticket_id=ticket.id,user_id=user.id,created_at=now())
                .on_conflict_do_nothing(index_elements=['ticket_id','user_id']))
        receipt = f'Вы присоединились к {ticket.number}. {public_url()}/tickets/{ticket.id}'
        return {'step':'done','receipt':receipt}, receipt
    if draft.get('step') in ('review', 'join_confirm'):
        match = re.fullmatch(r'присоединиться\s+(?:a-|а-)?(\d+)', command)
        if match:
            ticket_id = int(match[1])
            if ticket_id not in draft.get('similar_ids', []): return draft, 'Выберите номер из предложенных похожих проблем.'
            draft.update(step='join_confirm',join_id=ticket_id)
            return draft, f'Присоединиться к A-{ticket_id:03d}? Напишите «подтвердить». Новое обращение не будет создано, ваш черновик и вложения не передадутся в общую проблему.'
        if command == 'отправить':
            if draft.get('file_error'): return draft, 'Сначала повторите загрузку файла или напишите «без файлов».'
            category = db.get(Category, draft['category_id'])
            if not category or not category.is_active: return draft, preview(db, draft)
            ticket = create_from_draft(db, user, draft)
            receipt = f'Обращение {ticket.number} создано. {public_url()}/tickets/{ticket.id}'
            return {'step':'done','receipt':receipt}, receipt + '\nСледить за ответами можно в Модусе. Для входа используйте аккаунт Модуса; пароль Битрикса автоматически не переносится.'
        return draft, preview(db, draft)
    if command in ('да', 'отправить', 'подтвердить') or len(text.strip()) < 10: return draft, HELP
    if len(text) > 20000: return draft, 'Описание слишком длинное. Отправьте текст до 20 000 символов.'
    description = text.strip()
    title = description.splitlines()[0][:250]
    if len(title) < 3: title = 'Обращение из Битрикс24'
    cat_id, _ = rule_category(db, title, description)
    draft.update(step='category',description=description,title=title,type=rule_type(title, description),category_id=cat_id)
    return draft, category_prompt(db, draft)


def process_event(db, bot, event, api):
    event_id = int(event['eventId'])
    if db.scalar(select(BotReceipt.id).where(BotReceipt.bot_id == bot.id, BotReceipt.event_id == event_id)): return
    receipt = BotReceipt(bot_id=bot.id,event_id=event_id,state='ignored')
    db.add(receipt)
    data = event.get('data') or {}
    if event.get('type') != 'ONIMBOTV2MESSAGEADD': return
    if data.get('bot', {}).get('id') is not None and int(data['bot']['id']) != bot.external_id: return
    chat, remote, message = data.get('chat') or {}, data.get('user') or {}, data.get('message') or {}
    remote_id = int(remote.get('id') or 0)
    if (chat.get('type') != 'private' or active(remote.get('bot')) or active(remote.get('extranet'))
            or active(remote.get('connector')) or active(message.get('isSystem')) or remote_id <= 0
            or int(message.get('authorId') or 0) != remote_id
            or str(chat.get('dialogId')) != str(remote_id)):
        return
    user = mapped_user(db, bot.scope, remote_id)
    receipt.external_user_id, receipt.state = remote_id, 'pending'
    if not user or remote.get('active') is False:
        receipt.reply = 'Ваш аккаунт пока не связан с активным сотрудником Модуса. Попросите администратора синхронизировать сотрудников и проверить связь аккаунта.'
        return
    receipt.user_id = user.id
    dialog = db.scalar(select(BotDialog).where(BotDialog.bot_id == bot.id,BotDialog.external_user_id == remote_id).with_for_update())
    if not dialog:
        dialog = BotDialog(bot_id=bot.id, external_user_id=remote_id, user_id=user.id, draft='{}', updated_at=now())
        db.add(dialog)
    params = message.get('params') or {}
    ids = params.get('FILE_ID', []) if isinstance(params, dict) else []
    if not isinstance(ids, list) or len(ids) > 5 or any(not str(i).isdigit() for i in ids):
        receipt.reply = 'Пришлите до 5 файлов за раз.'
        return
    text = str(message.get('text') or '').strip()
    text = re.sub(r'\[FILE[^\]]*\].*?\[/FILE\]', '', text, flags=re.I | re.S).strip()
    draft, receipt.reply = respond(db, bot, user, dialog, text, [int(i) for i in ids], api)
    dialog.draft, dialog.updated_at = json.dumps(draft, ensure_ascii=False), now()


def poll(db, bot, api):
    result = api.call('imbot.v2.Event.get', {**credentials(bot),'offset':bot.cursor,'limit':20,'withUserEvents':False})['result']
    events = result.get('events', [])
    next_offset = int(result['nextOffset'])
    if next_offset < bot.cursor: raise BitrixError('BOT_INVALID_CURSOR')
    for event in events:
        try:
            with db.begin_nested():
                process_event(db, bot, event, api)
        except (ValueError, TypeError, KeyError):
            event_id = int(event['eventId'])
            if not db.scalar(select(BotReceipt.id).where(BotReceipt.bot_id == bot.id, BotReceipt.event_id == event_id)):
                db.add(BotReceipt(bot_id=bot.id, event_id=event_id, state='ignored', error='BOT_INVALID_EVENT'))
        db.commit()  # Ticket + dialog + receipt atomically committed before acknowledging the event.
    bot.cursor, bot.checked_at, bot.health = next_offset, now(), 'Работает · ожидает сообщения'
    db.commit()
    for receipt in db.scalars(select(BotReceipt).where(BotReceipt.bot_id == bot.id,BotReceipt.state == 'sending')):
        receipt.state, receipt.error = 'uncertain', 'BOT_REPLY_DELIVERY_UNKNOWN'
    db.commit()
    pending = db.scalars(select(BotReceipt).where(BotReceipt.bot_id == bot.id,
        BotReceipt.state.in_(['pending','error']),BotReceipt.attempts < 5,BotReceipt.next_at <= now()).order_by(BotReceipt.id).limit(10)).all()
    for receipt in pending:
        # Previously generated private previews must not be sent after the user's mapping changes.
        person = mapped_user(db, bot.scope, receipt.external_user_id)
        dialog = db.scalar(select(BotDialog).where(BotDialog.bot_id==bot.id,BotDialog.external_user_id==receipt.external_user_id))
        if receipt.user_id and (not person or person.id != receipt.user_id):
            receipt.state, receipt.reply = 'skipped', ''
            db.commit()
            continue
        receipt.state, receipt.attempts = 'sending', receipt.attempts + 1
        db.commit()
        try:
            response = api.call('imbot.v2.Chat.Message.send', {**credentials(bot), 'dialogId': str(receipt.external_user_id),
                'fields': {'message': receipt.reply[:20000], 'urlPreview': False}})['result']
            if not response.get('id'): raise BitrixError('BOT_REPLY_NOT_SENT')
            receipt.state, receipt.error = 'sent', None
            # Keep delivery audit, not private message copies, after successful sending.
            receipt.reply = ''
        except BitrixError as e:
            receipt.state, receipt.error = ('uncertain' if e.uncertain else 'error'), e.code
            receipt.next_at = now() + timedelta(seconds=30 * 2 ** receipt.attempts)
        db.commit()


def run_bot(db, realm, api):
    bot = db.scalar(select(BitrixBot).where(BitrixBot.scope == realm,BitrixBot.enabled.is_(True)))
    if not bot or not bot.external_id or realm == 'mock': return
    try:
        cleanup_drafts(db)
        db.commit()
        poll(db, bot, api)
    except BitrixError as e:
        db.rollback()
        bot.health, bot.checked_at = e.code, now()
        db.commit()


def cleanup_drafts(db):
    """Expire abandoned drafts and remove only unreferenced bot-upload files after 24h."""
    cutoff = now() - timedelta(hours=24)
    for dialog in db.scalars(select(BotDialog).where(BotDialog.updated_at < cutoff, BotDialog.draft != '{}')):
        dialog.draft = '{}'
    referenced = set(db.scalars(select(Attachment.stored_path)))
    for dialog in db.scalars(select(BotDialog).where(BotDialog.updated_at >= cutoff)):
        referenced.update(f['stored_path'] for f in json.loads(dialog.draft).get('files', []))
    if UPLOADS.is_dir():
        import time
        for path in UPLOADS.glob('bot_*'):
            if path.is_file() and not path.is_symlink() and str(path) not in referenced and path.stat().st_mtime < time.time() - 86400:
                path.unlink(missing_ok=True)
