# Модус Трейд — бэкенд (FastAPI)

## Запуск у себя

Нужен Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head             # создаёт локальную базу app.db (SQLite)
python seed.py                   # демо-данные, пароль у всех: demo1234
uvicorn app.main:app --reload --port 8000
```

Проверка: http://127.0.0.1:8000/api/health → `{"status":"ok"}`.
Фронтенд (репозиторий `modus-front`) по умолчанию ходит именно сюда.

Тесты: `pytest -q`.

## Как попадает на сервер

Пуш в `main` → GitHub Actions: тесты → выкладка на сервер → миграции → перезапуск →
проверка здоровья → копия в репозиторий `modus/backend`.
Если тесты упали — сервер не трогается. Статус во вкладке **Actions**.

Локальная `app.db` и `.env` в git не попадают, боевая база с вашей не смешивается.
Новые таблицы/поля — только через миграцию Alembic (`alembic revision --autogenerate -m "..."`),
на сервере она применится сама.
