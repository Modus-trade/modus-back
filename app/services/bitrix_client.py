"""Small REST adapter. Never surface webhook URLs or arbitrary response text in errors."""
import os
import re
import time
from urllib.parse import urlsplit
import httpx


class BitrixError(Exception):
    def __init__(self, code, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


def portal():
    parts = urlsplit(os.getenv('BITRIX_WEBHOOK_URL', ''))
    if parts.scheme != 'https' or not parts.hostname or not re.fullmatch(r'/rest/\d+/[A-Za-z0-9_-]+/?', parts.path):
        raise BitrixError('WEBHOOK_NOT_CONFIGURED')
    return f'https://{parts.netloc}'


def scope(mode):
    return 'mock' if mode == 'mock' else portal() if mode == 'real' else 'off'


class BitrixClient:
    def __init__(self):
        portal()
        self.base = os.environ['BITRIX_WEBHOOK_URL'].rstrip('/') + '/'
        self.last = 0.0

    def call(self, method, params=None):
        time.sleep(max(0, .55 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        try:
            response = httpx.post(self.base + method + '.json', json=params or {}, timeout=15, follow_redirects=False)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise BitrixError('NETWORK_OR_INVALID_RESPONSE', uncertain=True) from None
        if not isinstance(data, dict) or response.status_code >= 500:
            raise BitrixError('REMOTE_SERVER_ERROR', uncertain=True)
        if data.get('error'):
            code = str(data['error'])
            raise BitrixError(code if re.fullmatch(r'[A-Za-z0-9_\.:-]{1,100}', code) else 'API_ERROR')
        if response.status_code >= 400 or 'result' not in data:
            raise BitrixError('HTTP_ERROR', uncertain=True)
        return data

    def all(self, method):
        rows, start, seen = [], 0, set()
        while True:
            data = self.call(method, {'start': start})
            result = data['result']
            if not isinstance(result, list):
                raise BitrixError('INVALID_DIRECTORY')
            rows.extend(result)
            if 'next' not in data:
                return rows
            start = int(data['next'])
            if start in seen:
                raise BitrixError('INVALID_PAGINATION')
            seen.add(start)


class MockClient:
    def call(self, method, params=None):
        p = params or {}
        if method == 'profile': return {'result': {'ID': '9001'}}
        if method == 'scope': return {'result': ['user', 'department', 'task', 'im']}
        if method == 'user.get': return {'result': [
            {'ID': '9001', 'NAME': 'Алина', 'LAST_NAME': 'Демо', 'EMAIL': 'alina@bitrix.mock', 'ACTIVE': True, 'UF_DEPARTMENT': [901], 'WORK_POSITION': 'Руководитель IT'},
            {'ID': '9002', 'NAME': 'Данияр', 'LAST_NAME': 'Демо', 'EMAIL': 'daniyar@bitrix.mock', 'ACTIVE': True, 'UF_DEPARTMENT': [901], 'WORK_POSITION': 'Инженер'},
        ]}
        if method == 'department.get': return {'result': [{'ID': '901', 'NAME': 'IT · ДЕМО', 'UF_HEAD': '9001', 'PARENT': '0'}]}
        if method == 'tasks.task.list': return {'result': {'tasks': []}}
        if method == 'tasks.task.add': return {'result': {'task': {'id': p['fields']['XML_ID'].split('-')[-1]}}}
        if method == 'im.notify.personal.add': return {'result': 1}
        raise BitrixError('MOCK_METHOD_NOT_SUPPORTED')

    def all(self, method):
        return self.call(method)['result']


def client(mode):
    if mode == 'mock': return MockClient()
    if mode == 'real': return BitrixClient()
    raise BitrixError('INTEGRATION_OFF')
