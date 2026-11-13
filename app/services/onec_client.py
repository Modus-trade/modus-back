"""Read-only OData adapter: fixed collections, GET only, no redirects or raw errors."""
import os
import re
from urllib.parse import urlsplit
from uuid import UUID
import httpx

class OneCError(Exception):
    pass

DEFAULTS = {'items':'Catalog_Номенклатура', 'partners':'Catalog_Контрагенты',
            'purchase':'Document_ЗаказПоставщику', 'sale':'Document_ЗаказКлиента'}

def base_url():
    value = os.getenv('ONEC_ODATA_URL', '').rstrip('/')
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise OneCError('Не настроен HTTPS-адрес OData в backend')
    return value

def collection(kind):
    name = os.getenv('ONEC_ENTITY_'+kind.upper(), DEFAULTS[kind])
    if not re.fullmatch(r'(Catalog|Document)_[\w]+', name):
        raise OneCError('Некорректное имя коллекции 1С')
    return name

def field(name, default):
    value = os.getenv('ONEC_FIELD_'+name, default)
    if not re.fullmatch(r'\w+', value): raise OneCError('Некорректное имя поля 1С')
    return value

class ODataClient:
    def __init__(self):
        self.base = base_url()
        username, password = os.getenv('ONEC_USERNAME'), os.getenv('ONEC_PASSWORD')
        if not username or not password: raise OneCError('Не настроены учётные данные 1С')
        self.auth = (username, password)

    def all(self, kind):
        columns = ['Ref_Key','DeletionMark']
        if kind in ('items','partners'): columns += ['Description','Code','IsFolder']
        if kind == 'items': columns += [field('ARTICLE','Артикул')]
        if kind in ('purchase','sale'): columns += ['Number','Date',field('PARTNER','Контрагент_Key')]
        rows, seen = [], set()
        with httpx.Client(auth=self.auth, timeout=20, follow_redirects=False) as client:
            for page in range(100):
                try:
                    response = client.get(self.base+'/'+collection(kind), params={
                        '$format':'json','$select':','.join(columns),'$orderby':'Ref_Key',
                        '$top':200,'$skip':page*200})
                    if response.status_code in (401,403): raise OneCError('1С отказала в доступе: проверьте права учётной записи')
                    if response.status_code != 200: raise OneCError('Ошибка чтения 1С: проверьте публикацию, коллекции и поля')
                    data = response.json()
                    batch = data.get('value') if isinstance(data,dict) else None
                    if not isinstance(batch,list): raise OneCError('Неверный формат ответа OData')
                    for row in batch:
                        ref = str(UUID(row['Ref_Key']))
                        if ref in seen: raise OneCError('1С вернула повтор страницы; импорт отменён')
                        seen.add(ref)
                        if row.get('DeletionMark') is True or row.get('IsFolder') is True: continue
                        rows.append(row)
                    if len(batch)<200: return rows
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    raise OneCError('Не удалось прочитать OData: сеть или формат ответа') from None
        raise OneCError('Превышен лимит 20 000 записей на коллекцию; ограничьте публикацию 1С')
