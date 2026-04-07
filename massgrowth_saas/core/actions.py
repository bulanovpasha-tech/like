"""
core/actions.py — выполнение действий в Instagram.

Каждый метод:
  1. Проверяет лимиты через SafetyController.can_act()
  2. Проверяет блокировку через AntiBanHandler.is_account_blocked()
  3. Выполняет действие через AntiBanHandler.safe_call()
  4. Логирует результат в ActionLog
  5. Обновляет счётчик через SafetyController.record_action()
  6. Ждёт через SafetyController.wait()

ВАЖНО: Никаких "пачек" действий. Строго последовательно.

TODO: safe_comment() — модуль комментариев
TODO: safe_dm() — модуль автодиректа
TODO: safe_unfollow() — ротация подписок
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from instagrapi import Client

from core.anti_ban import AntiBanHandler
from core.safety import SafetyController
from db.models import ActionLog, ActionStatus, ActionType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

# Предупреждение, добавляемое ко всем ответам API
SAFETY_WARNING = (
    "Автоматизация может вызвать ограничения аккаунта. "
    "Рекомендуется режим Soft. Используйте на свой страх и риск."
)


class ActionExecutor:
    """
    Выполняет действия Instagram с проверкой лимитов и логированием.

    Args:
        account_id: ID аккаунта в БД.
        client: Авторизованный instagrapi Client.
        db: SQLAlchemy-сессия.
        safety: Экземпляр SafetyController.
        anti_ban: Экземпляр AntiBanHandler.
        config_path: Путь к config.yaml.
    """

    def __init__(
        self,
        account_id: int,
        client: Client,
        db: "Session",
        safety: SafetyController,
        anti_ban: AntiBanHandler,
        config_path: str = "config.yaml",
    ) -> None:
        self._account_id = account_id
        self._client = client
        self._db = db
        self._safety = safety
        self._anti_ban = anti_ban
        self._log = logger.bind(account_id=account_id)

    # ──────────────────────────────────────────────────────────────
    # Лайк поста
    # ──────────────────────────────────────────────────────────────

    async def safe_like(
        self,
        media_id: str,
        target_username: str | None = None,
        target_user_id: str | None = None,
        source: str | None = None,
        proxy_id: str | None = None,
    ) -> ActionStatus:
        """
        Безопасно ставит лайк на публикацию.

        Args:
            media_id: Instagram media ID.
            target_username: @username владельца поста (для логов).
            target_user_id: IG user ID владельца (для логов).
            source: Откуда взят пост: "geo:Moscow", "competitor:@user".
            proxy_id: Маскированный proxy ID для логов.

        Returns:
            ActionStatus с результатом операции.
        """
        action = ActionType.LIKE

        # Проверка блокировки аккаунта
        if self._anti_ban.is_account_blocked():
            self._log.warning("like_skipped_account_blocked", media_id=media_id)
            return ActionStatus.SKIPPED

        # Проверка лимита
        if not self._safety.can_act(action):
            return self._log_action(
                action=action,
                status=ActionStatus.LIMIT_REACHED,
                media_id=media_id,
                target_username=target_username,
                target_user_id=target_user_id,
                source=source,
                proxy_id=proxy_id,
            )

        self._log.info(
            "like_attempt",
            media_id=media_id,
            target=target_username,
            remaining=self._safety.get_remaining(action),
        )

        # Выполняем лайк через AntiBanHandler
        result, success = await self._anti_ban.safe_call(
            self._client.media_like, media_id=media_id
        )

        if not success or not result:
            status = ActionStatus.ERROR
            error_msg = "safe_call returned failure"
        else:
            status = ActionStatus.SUCCESS
            error_msg = None
            self._safety.record_action(action)

        logged_status = self._log_action(
            action=action,
            status=status,
            media_id=media_id,
            target_username=target_username,
            target_user_id=target_user_id,
            source=source,
            proxy_id=proxy_id,
            error_message=error_msg,
        )

        # Пауза после действия (только при успехе — при ошибке anti_ban уже ждал)
        if success:
            delay = await self._safety.wait()
            self._log.debug("like_done", media_id=media_id, delay=round(delay, 1))

        return logged_status

    # ──────────────────────────────────────────────────────────────
    # Просмотр сторис
    # ──────────────────────────────────────────────────────────────

    async def safe_view_story(
        self,
        user_id: str,
        story_pks: list[int],
        target_username: str | None = None,
        source: str | None = None,
        proxy_id: str | None = None,
    ) -> ActionStatus:
        """
        Безопасно просматривает сторис пользователя.

        Args:
            user_id: IG user ID владельца сторис.
            story_pks: Список PK сторис (из parser.py).
            target_username: @username для логов.
            source: Источник аудитории.
            proxy_id: Proxy ID для логов.

        Returns:
            ActionStatus с результатом.
        """
        action = ActionType.STORY_VIEW

        if self._anti_ban.is_account_blocked():
            return ActionStatus.SKIPPED

        if not self._safety.can_act(action):
            return self._log_action(
                action=action,
                status=ActionStatus.LIMIT_REACHED,
                target_user_id=user_id,
                target_username=target_username,
                source=source,
                proxy_id=proxy_id,
            )

        if not story_pks:
            self._log.debug("no_stories_to_view", user_id=user_id)
            return ActionStatus.SKIPPED

        self._log.info(
            "story_view_attempt",
            user_id=user_id,
            story_count=len(story_pks),
            target=target_username,
        )

        result, success = await self._anti_ban.safe_call(
            self._client.story_seen, story_pks=story_pks
        )

        if not success:
            status = ActionStatus.ERROR
        else:
            status = ActionStatus.SUCCESS
            self._safety.record_action(action)

        logged_status = self._log_action(
            action=action,
            status=status,
            target_user_id=user_id,
            target_username=target_username,
            source=source,
            proxy_id=proxy_id,
        )

        if success:
            delay = await self._safety.wait()
            self._log.debug("story_viewed", user_id=user_id, delay=round(delay, 1))

        return logged_status

    # ──────────────────────────────────────────────────────────────
    # Подписка
    # ──────────────────────────────────────────────────────────────

    async def safe_follow(
        self,
        user_id: str,
        target_username: str | None = None,
        source: str | None = None,
        proxy_id: str | None = None,
    ) -> ActionStatus:
        """
        Безопасно подписывается на пользователя.

        Args:
            user_id: IG user ID для подписки.
            target_username: @username для логов.
            source: Источник аудитории.
            proxy_id: Proxy ID для логов.

        Returns:
            ActionStatus с результатом.
        """
        action = ActionType.FOLLOW

        if self._anti_ban.is_account_blocked():
            return ActionStatus.SKIPPED

        if not self._safety.can_act(action):
            return self._log_action(
                action=action,
                status=ActionStatus.LIMIT_REACHED,
                target_user_id=user_id,
                target_username=target_username,
                source=source,
                proxy_id=proxy_id,
            )

        self._log.info(
            "follow_attempt",
            user_id=user_id,
            target=target_username,
            remaining=self._safety.get_remaining(action),
        )

        result, success = await self._anti_ban.safe_call(
            self._client.user_follow, user_id=user_id
        )

        if not success:
            status = ActionStatus.ERROR
        else:
            status = ActionStatus.SUCCESS
            self._safety.record_action(action)

        logged_status = self._log_action(
            action=action,
            status=status,
            target_user_id=user_id,
            target_username=target_username,
            source=source,
            proxy_id=proxy_id,
        )

        if success:
            delay = await self._safety.wait()
            self._log.debug("follow_done", user_id=user_id, delay=round(delay, 1))

        return logged_status

    # ──────────────────────────────────────────────────────────────
    # TODO: safe_unfollow()
    # TODO: safe_comment()
    # TODO: safe_dm()
    # ──────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────
    # Логирование
    # ──────────────────────────────────────────────────────────────

    def _log_action(
        self,
        action: ActionType,
        status: ActionStatus,
        media_id: str | None = None,
        target_username: str | None = None,
        target_user_id: str | None = None,
        source: str | None = None,
        proxy_id: str | None = None,
        delay_seconds: float | None = None,
        error_message: str | None = None,
    ) -> ActionStatus:
        """
        Записывает действие в таблицу ActionLog.
        Всегда возвращает переданный status для удобства цепочки.
        """
        log_entry = ActionLog(
            account_id=self._account_id,
            action_type=action,
            target_username=target_username,
            target_user_id=target_user_id,
            media_id=media_id,
            status=status,
            error_message=error_message,
            delay_seconds=delay_seconds,
            proxy_id=proxy_id,
            source=source,
            timestamp=datetime.utcnow(),
        )
        self._db.add(log_entry)
        self._db.commit()

        # Структурированный лог
        self._log.info(
            "action_logged",
            action=action.value,
            status=status.value,
            target=target_username,
            media_id=media_id,
            source=source,
        )

        return status

    # ──────────────────────────────────────────────────────────────
    # Статистика текущей сессии
    # ──────────────────────────────────────────────────────────────

    def get_session_stats(self) -> dict:
        """Возвращает статистику текущего дня из SafetyController."""
        return {
            **self._safety.get_daily_stats(),
            "warning": SAFETY_WARNING,
        }
