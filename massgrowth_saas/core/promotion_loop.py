"""
core/promotion_loop.py — главный цикл продвижения.

Связывает все компоненты в единый пайплайн:
  SessionManager → AudienceParser → ProfileFilter → ActionExecutor

Порядок действий на каждый профиль:
  1. Проверить, что задача не остановлена (флаг STOPPED в БД)
  2. Быстрая фильтрация (passes_short) — без дополнительных API-запросов
  3. Загрузить полный профиль (user_info) — «дорогой» запрос
  4. Полная фильтрация (passes)
  5. Получить сторис (если do_view_stories)
  6. Выполнить действия: view_story → like → follow
  7. Обновить счётчики задачи в БД
  8. Сохранить сессию каждые SAVE_SESSION_EVERY действий

Безопасность:
  - После каждого действия — пауза через SafetyController.wait()
  - При блокировке/лимите — немедленный выход из цикла
  - Каждые CHECK_STOP_EVERY итераций проверяем флаг остановки
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from core.actions import ActionExecutor, ActionStatus
from core.anti_ban import AntiBanHandler
from core.filter import ProfileFilter
from core.parser import AudienceParser
from core.safety import SafetyController
from core.session import SessionManager
from db.models import Account, ActionType, Task, TaskStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

# Сохранять сессию каждые N успешных действий
SAVE_SESSION_EVERY = 10

# Проверять флаг остановки каждые N итераций цикла
CHECK_STOP_EVERY = 5


async def promotion_loop(task_id: int, db: "Session") -> None:
    """
    Главный асинхронный цикл продвижения для одной задачи.

    Args:
        task_id: ID задачи в БД.
        db: SQLAlchemy-сессия (должна быть отдельной от API-сессии).

    Весь цикл выполняется в одном потоке.
    При остановке через /tasks/{id}/stop — безопасно завершается после текущего действия.
    """
    log = logger.bind(task_id=task_id)

    # ──────────────────────────────────────────────────────────────
    # 1. Загружаем задачу и аккаунт
    # ──────────────────────────────────────────────────────────────
    task = db.query(Task).filter_by(id=task_id).first()
    if not task:
        log.error("task_not_found")
        return

    account = db.query(Account).filter_by(id=task.account_id).first()
    if not account:
        log.error("account_not_found", account_id=task.account_id)
        _fail_task(task, db, "Account not found")
        return

    config = json.loads(task.config_snapshot or "{}")
    log = log.bind(account_id=account.id, username=account.username)

    do_like: bool = config.get("do_like", True)
    do_follow: bool = config.get("do_follow", True)
    do_view_stories: bool = config.get("do_view_stories", True)
    max_targets: int = config.get("max_targets", 50)
    location_ids: list[int] = config.get("location_ids", [])
    competitor_usernames: list[str] = config.get("competitor_usernames", [])

    if not location_ids and not competitor_usernames:
        _fail_task(task, db, "No location_ids or competitor_usernames specified")
        return

    log.info("promotion_loop_started", max_targets=max_targets)

    # ──────────────────────────────────────────────────────────────
    # 2. Инициализация компонентов
    # ──────────────────────────────────────────────────────────────
    session_mgr = SessionManager(account_id=account.id, db=db)

    try:
        client = session_mgr.get_client()
    except RuntimeError as e:
        log.error("client_init_failed", error=str(e))
        _fail_task(task, db, str(e))
        return

    safety = SafetyController(
        account_id=account.id,
        db=db,
        safety_mode=account.safety_mode,
        account_age_days=account.account_age_days,
    )
    anti_ban = AntiBanHandler(account_id=account.id, db=db)
    profile_filter = ProfileFilter()
    parser = AudienceParser(client=client, profile_filter=profile_filter)
    executor = ActionExecutor(
        account_id=account.id,
        client=client,
        db=db,
        safety=safety,
        anti_ban=anti_ban,
    )

    # ──────────────────────────────────────────────────────────────
    # 3. Главный цикл
    # ──────────────────────────────────────────────────────────────
    targets_processed = 0
    actions_done = 0
    iteration = 0

    # Собираем все источники аудитории в единую последовательность
    async for user_short, media_id, source in _iter_audience(
        parser, location_ids, competitor_usernames, log
    ):
        iteration += 1

        # Проверяем флаг остановки
        if iteration % CHECK_STOP_EVERY == 0:
            db.refresh(task)
            if task.status == TaskStatus.STOPPED:
                log.info("task_stop_flag_detected")
                break

        # Лимит целей
        if targets_processed >= max_targets:
            log.info("max_targets_reached", count=targets_processed)
            break

        # Если аккаунт заблокирован — стоп
        if anti_ban.is_account_blocked():
            log.warning("account_blocked_stopping_loop")
            break

        # Загружаем полный профиль (только для прошедших быстрый фильтр)
        user = parser.get_full_user_info(str(user_short.pk))
        if not user:
            continue

        # Полная фильтрация
        if not profile_filter.passes(user):
            continue

        log.info(
            "target_accepted",
            username=user.username,
            followers=user.follower_count,
            source=source,
        )
        targets_processed += 1

        # ── 3a. Просмотр сторис ──────────────────────────────────
        if do_view_stories and safety.can_act(ActionType.STORY_VIEW):
            story_pks = parser.get_user_story_pks(str(user.pk))

            if story_pks:
                # Проверяем наличие свежих сторис через фильтр
                from datetime import timezone
                story_times = []
                try:
                    stories = client.user_stories(user_id=user.pk)
                    story_times = [
                        s.taken_at for s in stories
                        if s.taken_at
                    ]
                except Exception:
                    story_times = []

                if profile_filter.check_has_recent_stories(str(user.pk), story_times):
                    status = await executor.safe_view_story(
                        user_id=str(user.pk),
                        story_pks=story_pks,
                        target_username=user.username,
                        source=source,
                        proxy_id=_mask_proxy(account.proxy),
                    )
                    if status == ActionStatus.SUCCESS:
                        actions_done += 1
                        task.story_views_done += 1
                        db.commit()

                    if status in (ActionStatus.BANNED, ActionStatus.LIMIT_REACHED):
                        if status == ActionStatus.BANNED:
                            break

        # ── 3b. Лайк поста ───────────────────────────────────────
        if do_like and media_id and safety.can_act(ActionType.LIKE):
            status = await executor.safe_like(
                media_id=media_id,
                target_username=user.username,
                target_user_id=str(user.pk),
                source=source,
                proxy_id=_mask_proxy(account.proxy),
            )
            if status == ActionStatus.SUCCESS:
                actions_done += 1
                task.likes_done += 1
                db.commit()

            if status == ActionStatus.BANNED:
                break

        # ── 3c. Подписка ─────────────────────────────────────────
        if do_follow and safety.can_act(ActionType.FOLLOW):
            status = await executor.safe_follow(
                user_id=str(user.pk),
                target_username=user.username,
                source=source,
                proxy_id=_mask_proxy(account.proxy),
            )
            if status == ActionStatus.SUCCESS:
                actions_done += 1
                task.follows_done += 1
                db.commit()

            if status == ActionStatus.BANNED:
                break

        # ── Обновляем общий счётчик ──────────────────────────────
        task.actions_done = actions_done
        db.commit()

        # ── Сохраняем сессию периодически ────────────────────────
        if actions_done > 0 and actions_done % SAVE_SESSION_EVERY == 0:
            session_mgr.save_session()
            log.debug("session_checkpoint_saved", actions_done=actions_done)

    # ──────────────────────────────────────────────────────────────
    # 4. Финализация
    # ──────────────────────────────────────────────────────────────
    session_mgr.save_session()

    # Не перезаписываем STOPPED статус
    db.refresh(task)
    if task.status not in (TaskStatus.STOPPED, TaskStatus.FAILED):
        task.status = TaskStatus.COMPLETED
        task.finished_at = datetime.utcnow()
        db.commit()

    log.info(
        "promotion_loop_finished",
        targets_processed=targets_processed,
        actions_done=actions_done,
        status=task.status.value,
    )


# ──────────────────────────────────────────────────────────────────
# Генератор аудитории из всех источников
# ──────────────────────────────────────────────────────────────────

async def _iter_audience(
    parser: AudienceParser,
    location_ids: list[int],
    competitor_usernames: list[str],
    log,
):
    """
    Асинхронный генератор: объединяет аудиторию из геолокаций и конкурентов.

    Yields:
        (UserShort, media_id | None, source_label)
    """
    # Геолокации
    for loc_id in location_ids:
        source = f"geo:{loc_id}"
        log.info("parsing_source", source=source)

        # parser.get_users_by_location — синхронный генератор, запускаем в executor
        loop = asyncio.get_event_loop()
        users = await loop.run_in_executor(
            None,
            lambda lid=loc_id: list(parser.get_users_by_location(lid)),
        )
        for user_short, media_id in users:
            yield user_short, media_id, source
            # Маленькая уступка event loop между итерациями
            await asyncio.sleep(0)

    # Конкуренты
    for competitor in competitor_usernames:
        source = f"competitor:@{competitor}"
        log.info("parsing_source", source=source)

        loop = asyncio.get_event_loop()
        users = await loop.run_in_executor(
            None,
            lambda c=competitor: list(parser.get_followers_of_competitor(c)),
        )
        for user_short in users:
            yield user_short, None, source
            await asyncio.sleep(0)


# ──────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────────

def _fail_task(task: Task, db: "Session", reason: str) -> None:
    task.status = TaskStatus.FAILED
    task.error_message = reason
    task.finished_at = datetime.utcnow()
    db.commit()


def _mask_proxy(proxy: str | None) -> str | None:
    """Возвращает host:port без credentials для логов."""
    if not proxy:
        return None
    try:
        if "@" in proxy:
            return proxy.split("@")[-1]
        return proxy.split("//")[-1]
    except Exception:
        return "proxy"
