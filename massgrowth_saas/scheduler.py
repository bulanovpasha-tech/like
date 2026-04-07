"""
scheduler.py — APScheduler планировщик фоновых задач.

Задачи:
  1. daily_reset     — сброс суточных счётчиков (00:05 UTC каждый день)
  2. resume_accounts — проверка аккаунтов на паузе (каждые 30 мин)
  3. task_runner     — запуск pending задач (каждые N секунд)

TODO: добавить ротацию логов.
TODO: добавить heartbeat для мониторинга (Prometheus/Healthcheck).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import structlog
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db.database import SessionLocal
from db.models import Account, AccountStatus, DailyLimit, Task, TaskStatus
from core.safety import SafetyController

logger = structlog.get_logger(__name__)


def _load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────────
# Джобы планировщика
# ──────────────────────────────────────────────────────────────────

def job_daily_reset() -> None:
    """
    Сбрасывает суточные счётчики всех аккаунтов.
    Вызывается в 00:05 UTC.
    Фактически — логирует сброс; новые записи DailyLimit создаются лениво
    при первом вызове _get_or_create_daily_limit().
    """
    db = SessionLocal()
    try:
        accounts = db.query(Account).filter(
            Account.status.in_([AccountStatus.ACTIVE, AccountStatus.PAUSED])
        ).all()

        for account in accounts:
            controller = SafetyController(
                account_id=account.id,
                db=db,
                safety_mode=account.safety_mode,
                account_age_days=account.account_age_days,
            )
            controller.reset_daily()

        logger.info("daily_reset_done", accounts_reset=len(accounts))
    except Exception as e:
        logger.exception("daily_reset_failed", error=str(e))
    finally:
        db.close()


def job_resume_accounts() -> None:
    """
    Проверяет аккаунты на паузе и возобновляет те, у которых истекло время ожидания.
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        paused_accounts = db.query(Account).filter(
            Account.status == AccountStatus.PAUSED,
            Account.paused_until.isnot(None),
            Account.paused_until <= now,
        ).all()

        for account in paused_accounts:
            account.status = AccountStatus.ACTIVE
            account.paused_until = None
            account.consecutive_errors = 0
            db.commit()
            logger.info("account_resumed", account_id=account.id, username=account.username)

    except Exception as e:
        logger.exception("resume_accounts_failed", error=str(e))
    finally:
        db.close()


def job_run_pending_tasks() -> None:
    """
    Запускает задачи в статусе PENDING.
    Ограничивает количество одновременно работающих задач (max_concurrent_accounts).

    TODO: полная интеграция с promotion_loop из api/main.py.
    """
    db = SessionLocal()
    try:
        cfg = _load_config()
        max_concurrent = cfg.get("scheduler", {}).get("max_concurrent_accounts", 3)

        running_count = db.query(Task).filter_by(status=TaskStatus.RUNNING).count()
        slots_available = max_concurrent - running_count

        if slots_available <= 0:
            logger.debug("no_slots_for_new_tasks", running=running_count, max=max_concurrent)
            return

        pending_tasks = (
            db.query(Task)
            .filter_by(status=TaskStatus.PENDING)
            .order_by(Task.created_at)
            .limit(slots_available)
            .all()
        )

        for task in pending_tasks:
            account = db.query(Account).filter_by(id=task.account_id).first()
            if not account or account.status != AccountStatus.ACTIVE:
                continue

            logger.info(
                "scheduler_starting_task",
                task_id=task.id,
                account_id=task.account_id,
            )
            # TODO: вместо прямого вызова — отправить в очередь задач
            # Пока — запускаем через _run_task_background из api/main.py
            from api.main import _run_task_background
            import threading
            thread = threading.Thread(
                target=_run_task_background,
                args=(task.id,),
                daemon=True,
                name=f"task-{task.id}",
            )
            thread.start()

    except Exception as e:
        logger.exception("run_pending_tasks_failed", error=str(e))
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────
# Создание и запуск планировщика
# ──────────────────────────────────────────────────────────────────

def create_scheduler(config_path: str = "config.yaml") -> BackgroundScheduler:
    """
    Создаёт и настраивает BackgroundScheduler.

    Returns:
        Настроенный, но ещё не запущенный планировщик.
    """
    cfg = _load_config(config_path)
    sched_cfg = cfg.get("scheduler", {})

    reset_time = sched_cfg.get("daily_reset_time", "00:05")
    reset_hour, reset_minute = reset_time.split(":")

    check_interval = sched_cfg.get("task_check_interval_seconds", 60)

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,       # Пропустить пропущенные запуски
            "max_instances": 1,     # Только один экземпляр каждой джобы
            "misfire_grace_time": 300,
        },
    )

    # 1. Суточный сброс лимитов
    scheduler.add_job(
        job_daily_reset,
        trigger=CronTrigger(hour=int(reset_hour), minute=int(reset_minute), timezone="UTC"),
        id="daily_reset",
        name="Сброс суточных лимитов",
        replace_existing=True,
    )

    # 2. Возобновление аккаунтов после паузы
    scheduler.add_job(
        job_resume_accounts,
        trigger=IntervalTrigger(minutes=30),
        id="resume_accounts",
        name="Возобновление аккаунтов после паузы",
        replace_existing=True,
    )

    # 3. Запуск pending задач
    scheduler.add_job(
        job_run_pending_tasks,
        trigger=IntervalTrigger(seconds=check_interval),
        id="run_pending_tasks",
        name="Запуск ожидающих задач",
        replace_existing=True,
    )

    logger.info(
        "scheduler_created",
        daily_reset_time=reset_time,
        check_interval_s=check_interval,
    )

    return scheduler
