import os
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
engine = create_engine(
    os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}"),
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def foreign_keys(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
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
