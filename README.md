# Модус Трейд — бэкенд

FastAPI-сервер системы обратной связи сотрудников. Через него работают [сайт](https://github.com/Modus-trade/modus-front) и iOS-приложение.
Демо: https://modus-trade.makquiz.site · Полный проект и документация: [Modus-trade/modus](https://github.com/Modus-trade/modus)

## Запуск у себя

Нужен Python 3.12+. Настраивать ничего не нужно: без `.env` используется локальная SQLite (`app.db`).

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head               # создать базу
python seed.py                     # демо-данные, пароль у всех: demo1234
uvicorn app.main:app --reload --port 8000
```

- Проверка: http://127.0.0.1:8000/api/health → `{"status":"ok"}`
- Документация API (Swagger): http://127.0.0.1:8000/docs
- Сайт из `modus-front` по умолчанию подключается именно сюда.

Тесты: `pytest -q` (временная база, ваши данные не трогаются).

## Структура

```text
app/
  main.py              приложение, подключение роутеров
  models.py            модели SQLAlchemy 2
  schemas.py           валидация Pydantic v2
  security.py          JWT-сессии, пароли, проверка ролей
  realtime.py          WebSocket-хаб; события уходят после коммита
  routers/             auth, tickets, problems, notifications, analytics, admin
  services/            права и переходы статусов, похожие проблемы,
                       центр внимания, уведомления, живые события
alembic/versions/      миграции базы
seed.py, dataset.json  демо-данные из датасета заказчика
tests/                 интеграционные тесты API
scripts/deploy.sh      выкладка на сервере (запускает GitHub Actions)
```

## Основные эндпоинты

| Метод | Путь | Что делает |
| --- | --- | --- |
| POST | `/api/auth/login`, `/api/auth/logout` · GET `/api/auth/me` | вход, выход, текущий пользователь |
| GET/POST | `/api/tickets` | список с фильтрами и создание обращения |
| GET/PATCH | `/api/tickets/{id}` | карточка, смена статуса, приоритета, ответственного, результата |
| POST | `/api/tickets/{id}/comments`, `/attachments` | комментарий или уточнение, файл |
| POST | `/api/problems/similar`, `/api/problems/{id}/join` | похожие обращения, присоединиться |
| GET/POST | `/api/notifications`, `/read-all`, `/{id}/read` | уведомления |
| WS | `/api/ws` | живые события (cookie сайта или токен из `GET /api/ws-token` для приложения) |
| GET | `/api/analytics/summary`, `/api/analytics/attention` | аналитика, центр внимания |
| … | `/api/categories`, `/api/users` | администрирование |

Полный список с параметрами есть в Swagger.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DATABASE_URL` | SQLite `app.db` | строка подключения (на сервере PostgreSQL) |
| `JWT_SECRET` | случайный при запуске | задайте, чтобы сессии не сбрасывались при перезапуске |
| `COOKIE_SECURE` | `false` | `true` при HTTPS |
| `ALLOWED_ORIGINS` | локальные адреса | адреса сайта через запятую |

Пример лежит в `.env.example`. Файлы `.env` и `app.db` в git не попадают, боевая база хранится только на сервере.

## Деплой

Пуш в `main` запускает GitHub Actions:
1. тесты;
2. выкладка на сервер, `alembic upgrade head`, перезапуск;
3. проверка `/api/health`;
4. копия кода в `Modus-trade/modus/backend`.

Если тесты падают, сервер не меняется. Статус виден во вкладке **Actions**.

Изменили модели — создайте миграцию: `alembic revision --autogenerate -m "что изменилось"`. На сервере она применится сама.
