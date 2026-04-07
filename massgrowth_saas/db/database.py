"""
db/database.py — фабрика engine и сессий SQLAlchemy.
Поддерживает SQLite (MVP) и PostgreSQL (продакшн) без изменения кода.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------
# Путь к БД
# ------------------------------------------------------------------
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/massgrowth.db")

# Neon / Supabase отдают URL с префиксом "postgres://" или "postgresql://"
# SQLAlchemy с psycopg2 требует "postgresql+psycopg2://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# Для SQLite — создаём директорию автоматически
if DATABASE_URL.startswith("sqlite"):
    db_path = DATABASE_URL.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

# connect_args нужны только для SQLite (многопоточность)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,          # True — выводит SQL в лог (только для отладки)
    pool_pre_ping=True,  # Проверка соединения перед каждым запросом
)

# Включаем WAL-режим для SQLite — улучшает конкурентный доступ
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""
    pass


def get_db():
    """
    Dependency для FastAPI: выдаёт сессию БД и гарантирует её закрытие.

    Использование:
        @app.get("/")
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    """Создаёт все таблицы. Вызывается при старте приложения."""
    from db.models import Account, DailyLimit, ActionLog, Task  # noqa: F401
    Base.metadata.create_all(bind=engine)
