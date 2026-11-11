import os
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file():
    """Подхватывает backend/.env (DATABASE_URL и т. п.), не перекрывая уже заданные переменные."""
    path = BASE_DIR / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env_file()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}")
IS_SQLITE = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
    # Проверка соединения на каждом запросе стоит лишний поход к удалённой базе;
    # вместо неё переоткрываем соединения раз в 30 минут.
    pool_recycle=1800,
)


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        # Регистронезависимый поиск по кириллице (в PostgreSQL работает из коробки).
        connection.create_function(
            "lower",
            1,
            lambda value: value.lower() if isinstance(value, str) else value,
            deterministic=True,
        )


SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
