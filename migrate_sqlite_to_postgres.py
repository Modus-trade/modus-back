"""Перенос данных из локальной SQLite (app.db) в базу из DATABASE_URL (PostgreSQL).

Запуск: .venv/bin/python migrate_sqlite_to_postgres.py [путь/к/app.db]
Схему в целевой базе заранее создаёт `alembic upgrade head`. Целевые таблицы должны быть пустыми.
"""
import sys
from pathlib import Path
from sqlalchemy import create_engine, func, select, text
from app.database import Base, engine as target
from app import models  # noqa: F401

source_path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "app.db")
source = create_engine(f"sqlite:///{source_path}")
tables = Base.metadata.sorted_tables  # родительские таблицы раньше дочерних

with source.connect() as src, target.begin() as dst:
    for table in tables:
        if dst.execute(select(func.count()).select_from(table)).scalar():
            sys.exit(f"Таблица {table.name} в целевой базе не пуста — перенос остановлен.")
    for table in tables:
        rows = [dict(r._mapping) for r in src.execute(select(table))]
        if rows:
            dst.execute(table.insert(), rows)
        if "id" in table.c and target.dialect.name == "postgresql":
            dst.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"
            ))
        print(f"{table.name}: {len(rows)}")
print("Готово.")
