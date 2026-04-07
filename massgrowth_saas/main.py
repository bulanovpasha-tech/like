"""
main.py — точка входа MassGrowth SaaS.

Запускает:
  1. FastAPI приложение (через Uvicorn)
  2. APScheduler планировщик фоновых задач
  3. Создаёт таблицы БД при первом запуске

Запуск:
    python main.py
    # или через Docker:
    docker compose up
"""

from __future__ import annotations

import logging
import os
import signal
import sys

import structlog
import uvicorn
from dotenv import load_dotenv

# Загружаем .env до любых импортов, использующих os.getenv
load_dotenv()

from api.main import app
from db.database import create_tables
from scheduler import create_scheduler

# ──────────────────────────────────────────────────────────────────
# Настройка структурного логирования
# ──────────────────────────────────────────────────────────────────

def configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ──────────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────────

_scheduler = None


def _handle_shutdown(signum, frame):  # noqa: ANN001
    log = structlog.get_logger("main")
    log.info("shutdown_signal_received", signal=signum)
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")
    sys.exit(0)


# ──────────────────────────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────────────────────────

def main() -> None:
    global _scheduler

    configure_logging()
    log = structlog.get_logger("main")

    log.info("massgrowth_starting", version="0.1.0")

    # 1. Создаём таблицы БД
    create_tables()
    log.info("database_ready")

    # 2. Запускаем планировщик
    _scheduler = create_scheduler()
    _scheduler.start()
    log.info("scheduler_started")

    # 3. Graceful shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # 4. Запускаем FastAPI
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    log.info("api_starting", host=host, port=port)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
