"""
api/main.py — FastAPI приложение.

Endpoints:
  POST /accounts          — добавить аккаунт
  POST /tasks/start       — запустить задачу продвижения
  POST /tasks/{id}/stop   — остановить задачу
  GET  /tasks/{id}/status — статус задачи
  GET  /stats/{account_id}— статистика аккаунта за день
  GET  /accounts          — список аккаунтов
  GET  /health            — healthcheck

Все ответы содержат поле warning с предупреждением об автоматизации.
"""

from __future__ import annotations

import json
from datetime import datetime, date
from typing import Annotated

import os

import structlog
import yaml
from fastapi import FastAPI, Depends, HTTPException, status, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from core.crypto import encrypt_password
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from db.database import get_db, create_tables
from db.models import (
    Account, AccountStatus, ActionLog, ActionType,
    ActionStatus, DailyLimit, Task, TaskStatus,
)
from core.constants import SAFETY_WARNING

logger = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────
# Инициализация FastAPI
# ──────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="MassGrowth SaaS",
        description="Безопасная автоматизация Instagram для beauty/wellness мастеров",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS: берём список разрешённых origins из .env
    # Формат: ALLOWED_ORIGINS=http://localhost:3000,https://app.example.com
    # Если переменная не задана — разрешаем только localhost (безопасный дефолт)
    raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8080")
    allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=True,
    )

    # Статические файлы (дашборд)
    _static = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.isdir(_static):
        app.mount("/static", StaticFiles(directory=_static), name="static")

    @app.on_event("startup")
    async def startup():
        create_tables()
        logger.info("app_started", version="0.1.0")

    return app


app = create_app()

# ──────────────────────────────────────────────────────────────────
# Pydantic схемы запросов
# ──────────────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6)
    proxy: str | None = Field(
        default=None,
        description="Прокси в формате http://user:pass@host:port",
    )
    safety_mode: str = Field(default="soft", pattern="^(soft|strict)$")
    account_age_days: int = Field(default=0, ge=0)

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, v: str | None) -> str | None:
        if v and not v.startswith(("http://", "https://", "socks5://")):
            raise ValueError("Proxy must start with http://, https://, or socks5://")
        return v


class TaskCreate(BaseModel):
    account_id: int = Field(..., gt=0)
    location_ids: list[int] = Field(default_factory=list)
    competitor_usernames: list[str] = Field(default_factory=list)
    do_like: bool = True
    do_follow: bool = True
    do_view_stories: bool = True
    max_targets: int = Field(default=50, ge=1, le=200)

    @field_validator("competitor_usernames")
    @classmethod
    def clean_usernames(cls, v: list[str]) -> list[str]:
        return [u.lstrip("@").strip() for u in v]


# ──────────────────────────────────────────────────────────────────
# Pydantic схемы ответов
# ──────────────────────────────────────────────────────────────────

class WarningMixin(BaseModel):
    warning: str = SAFETY_WARNING


class AccountResponse(WarningMixin):
    id: int
    username: str
    status: AccountStatus
    safety_mode: str
    proxy_set: bool
    account_age_days: int
    created_at: datetime

    class Config:
        from_attributes = True


class TaskResponse(WarningMixin):
    id: int
    account_id: int
    status: TaskStatus
    actions_done: int
    likes_done: int
    follows_done: int
    story_views_done: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    class Config:
        from_attributes = True


class DailyStatsResponse(WarningMixin):
    account_id: int
    date: date
    likes: int
    likes_limit: int
    follows: int
    follows_limit: int
    story_views: int
    story_views_limit: int
    unfollows: int
    unfollows_limit: int
    total_errors: int
    total_actions: int


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    timestamp: datetime


# ──────────────────────────────────────────────────────────────────
# Healthcheck
# ──────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def dashboard():
    """Главная страница — визуальный дашборд."""
    _index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "index.html")
    try:
        with open(_index, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Dashboard not found</h1><p>Path: " + _index + "</p>")


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    return HealthResponse(timestamp=datetime.utcnow())


# ──────────────────────────────────────────────────────────────────
# Аккаунты
# ──────────────────────────────────────────────────────────────────

@app.post(
    "/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["accounts"],
)
def add_account(
    payload: AccountCreate,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Добавляет Instagram-аккаунт в систему.
    Пароль шифруется через Fernet (SECRET_KEY из .env) перед сохранением в БД.
    """
    existing = db.query(Account).filter_by(username=payload.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account @{payload.username} already exists",
        )

    try:
        encrypted_password = encrypt_password(payload.password)
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Encryption error: {e}. Check SECRET_KEY in .env",
        )

    account = Account(
        username=payload.username,
        password_encrypted=encrypted_password,
        proxy=payload.proxy,
        safety_mode=payload.safety_mode,
        account_age_days=payload.account_age_days,
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    logger.info("account_added", username=payload.username, safety_mode=payload.safety_mode)

    return AccountResponse(
        id=account.id,
        username=account.username,
        status=account.status,
        safety_mode=account.safety_mode,
        proxy_set=bool(account.proxy),
        account_age_days=account.account_age_days,
        created_at=account.created_at,
    )


@app.get("/accounts", response_model=list[AccountResponse], tags=["accounts"])
def list_accounts(db: Annotated[Session, Depends(get_db)]):
    """Возвращает список всех аккаунтов."""
    accounts = db.query(Account).all()
    return [
        AccountResponse(
            id=a.id,
            username=a.username,
            status=a.status,
            safety_mode=a.safety_mode,
            proxy_set=bool(a.proxy),
            account_age_days=a.account_age_days,
            created_at=a.created_at,
        )
        for a in accounts
    ]


@app.get("/accounts/{account_id}", response_model=AccountResponse, tags=["accounts"])
def get_account(account_id: int, db: Annotated[Session, Depends(get_db)]):
    """Возвращает информацию об аккаунте."""
    account = db.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return AccountResponse(
        id=account.id,
        username=account.username,
        status=account.status,
        safety_mode=account.safety_mode,
        proxy_set=bool(account.proxy),
        account_age_days=account.account_age_days,
        created_at=account.created_at,
    )


# ──────────────────────────────────────────────────────────────────
# Задачи
# ──────────────────────────────────────────────────────────────────

@app.post(
    "/tasks/start",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
)
def start_task(
    payload: TaskCreate,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Создаёт и запускает задачу продвижения для аккаунта.

    Задача выполняется в фоне (BackgroundTasks).
    Для продакшна — перенести в Celery или APScheduler worker.
    """
    account = db.query(Account).filter_by(id=payload.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if account.status not in (AccountStatus.ACTIVE,):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Account is {account.status.value}, cannot start task",
        )

    # Проверяем, нет ли уже активной задачи
    active_task = (
        db.query(Task)
        .filter_by(account_id=payload.account_id, status=TaskStatus.RUNNING)
        .first()
    )
    if active_task:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task {active_task.id} is already running for this account",
        )

    task = Task(
        account_id=payload.account_id,
        config_snapshot=json.dumps(payload.model_dump()),
        status=TaskStatus.PENDING,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Запуск в фоне
    background_tasks.add_task(_run_task_background, task.id)

    logger.info("task_created", task_id=task.id, account_id=payload.account_id)

    return TaskResponse(
        id=task.id,
        account_id=task.account_id,
        status=task.status,
        actions_done=task.actions_done,
        likes_done=task.likes_done,
        follows_done=task.follows_done,
        story_views_done=task.story_views_done,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


@app.post("/tasks/{task_id}/stop", response_model=TaskResponse, tags=["tasks"])
def stop_task(task_id: int, db: Annotated[Session, Depends(get_db)]):
    """
    Останавливает задачу.
    Фактически меняет статус на STOPPED — воркер проверяет флаг и завершается.
    """
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot stop task with status {task.status.value}",
        )

    task.status = TaskStatus.STOPPED
    task.finished_at = datetime.utcnow()
    db.commit()

    logger.info("task_stopped", task_id=task_id)

    return TaskResponse(
        id=task.id,
        account_id=task.account_id,
        status=task.status,
        actions_done=task.actions_done,
        likes_done=task.likes_done,
        follows_done=task.follows_done,
        story_views_done=task.story_views_done,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


@app.get("/tasks/{task_id}/status", response_model=TaskResponse, tags=["tasks"])
def get_task_status(task_id: int, db: Annotated[Session, Depends(get_db)]):
    """Возвращает текущий статус задачи."""
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        id=task.id,
        account_id=task.account_id,
        status=task.status,
        actions_done=task.actions_done,
        likes_done=task.likes_done,
        follows_done=task.follows_done,
        story_views_done=task.story_views_done,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


# ──────────────────────────────────────────────────────────────────
# Статистика
# ──────────────────────────────────────────────────────────────────

@app.get("/stats/{account_id}", response_model=DailyStatsResponse, tags=["stats"])
def get_stats(account_id: int, db: Annotated[Session, Depends(get_db)]):
    """
    Возвращает статистику действий за сегодня для аккаунта.
    Включает лимиты, счётчики ошибок и предупреждение.
    """
    account = db.query(Account).filter_by(id=account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    today = date.today()
    daily = (
        db.query(DailyLimit)
        .filter_by(account_id=account_id, date=today)
        .first()
    )

    # Ошибки сегодня
    error_count = (
        db.query(ActionLog)
        .filter(
            ActionLog.account_id == account_id,
            ActionLog.status == ActionStatus.ERROR,
            ActionLog.timestamp >= datetime.combine(today, datetime.min.time()),
        )
        .count()
    )

    # Всего действий сегодня
    total_actions = (
        db.query(ActionLog)
        .filter(
            ActionLog.account_id == account_id,
            ActionLog.timestamp >= datetime.combine(today, datetime.min.time()),
        )
        .count()
    )

    return DailyStatsResponse(
        account_id=account_id,
        date=today,
        likes=daily.likes_count if daily else 0,
        likes_limit=daily.likes_limit if daily else 0,
        follows=daily.follows_count if daily else 0,
        follows_limit=daily.follows_limit if daily else 0,
        story_views=daily.story_views_count if daily else 0,
        story_views_limit=daily.story_views_limit if daily else 0,
        unfollows=daily.unfollows_count if daily else 0,
        unfollows_limit=daily.unfollows_limit if daily else 0,
        total_errors=error_count,
        total_actions=total_actions,
    )


# ──────────────────────────────────────────────────────────────────
# Vercel Cron endpoints (заменяют APScheduler в serverless среде)
# ──────────────────────────────────────────────────────────────────

@app.post("/scheduler/daily-reset", tags=["scheduler"], include_in_schema=False)
def cron_daily_reset():
    """
    Вызывается Vercel Cron в 00:05 UTC.
    В локальном Docker-деплое этот endpoint не используется (APScheduler делает сам).
    """
    from scheduler import job_daily_reset
    job_daily_reset()
    return {"ok": True, "job": "daily_reset"}


@app.post("/scheduler/resume-accounts", tags=["scheduler"], include_in_schema=False)
def cron_resume_accounts():
    """
    Вызывается Vercel Cron каждые 30 минут.
    """
    from scheduler import job_resume_accounts
    job_resume_accounts()
    return {"ok": True, "job": "resume_accounts"}


# ──────────────────────────────────────────────────────────────────
# Фоновая задача — полный цикл продвижения
# ──────────────────────────────────────────────────────────────────

def _run_task_background(task_id: int) -> None:
    """
    Запускает полный цикл продвижения в отдельном потоке.

    Создаёт собственный asyncio event loop (потокобезопасно),
    свою DB-сессию и делегирует всё в promotion_loop.
    """
    import asyncio
    from db.database import SessionLocal
    from core.promotion_loop import promotion_loop

    db = SessionLocal()
    try:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task:
            logger.error("run_task_background_no_task", task_id=task_id)
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        db.commit()

        logger.info("task_background_started", task_id=task_id, account_id=task.account_id)

        # Создаём новый event loop для этого потока
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(promotion_loop(task_id=task_id, db=db))
        finally:
            loop.close()

    except Exception as e:
        logger.exception("task_background_failed", task_id=task_id, error=str(e))
        db.rollback()
        task = db.query(Task).filter_by(id=task_id).first()
        if task and task.status not in (TaskStatus.STOPPED, TaskStatus.COMPLETED):
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            task.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
