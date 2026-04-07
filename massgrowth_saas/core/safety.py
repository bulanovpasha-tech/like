"""
core/safety.py — SafetyController.

Отвечает за:
  - Проверку суточных лимитов (can_act)
  - Человекоподобные задержки (wait)
  - Обновление счётчиков после действия (record_action)
  - Расчёт лимитов с учётом возраста аккаунта (compute_limits)

Никакой прямой работы с Instagram API здесь нет — только логика безопасности.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog
import yaml

from db.models import ActionType, DailyLimit

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)


def _load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class SafetyController:
    """
    Контроллер безопасности для одного аккаунта.

    Args:
        account_id: ID аккаунта в БД.
        db: SQLAlchemy-сессия.
        safety_mode: "soft" | "strict". Если None — читается из config.yaml.
        account_age_days: Возраст аккаунта в днях (влияет на множитель лимитов).
        config_path: Путь к config.yaml.
    """

    def __init__(
        self,
        account_id: int,
        db: "Session",
        safety_mode: str | None = None,
        account_age_days: int = 90,
        config_path: str = "config.yaml",
    ) -> None:
        self._account_id = account_id
        self._db = db
        self._config = _load_config(config_path)
        self._mode = safety_mode or self._config.get("safety_mode", "soft")
        self._account_age_days = account_age_days

        self._delays = self._config["delays"]
        self._limits_raw = self._config["limits"][self._mode]

        # Вычисляем лимиты с учётом возраста аккаунта
        self._limits = self.compute_limits()

        self._log = logger.bind(account_id=account_id, safety_mode=self._mode)

    # ──────────────────────────────────────────────────────────────
    # Вычисление лимитов с учётом возраста аккаунта
    # ──────────────────────────────────────────────────────────────

    def compute_limits(self) -> dict[str, int]:
        """
        Рассчитывает суточные лимиты с поправкой на возраст аккаунта.

        Возраст берётся из account_age_days, множитель из config.yaml.
        Пример: новый аккаунт (0 дней) × 0.2 = 20% от базового лимита.

        Returns:
            Словарь {action_name: limit_int}
        """
        age_multipliers: dict[str, float] = self._config.get("account_age_multiplier", {})

        # Находим подходящий множитель (наибольший порог, не превышающий возраст)
        multiplier = 1.0
        for threshold_str, mult in sorted(age_multipliers.items(), key=lambda x: int(x[0])):
            if self._account_age_days >= int(threshold_str):
                multiplier = float(mult)

        computed = {}
        for key, base_value in self._limits_raw.items():
            computed[key] = max(1, int(base_value * multiplier))

        self._log.debug(
            "limits_computed",
            age_days=self._account_age_days,
            multiplier=multiplier,
            limits=computed,
        )
        return computed

    # ──────────────────────────────────────────────────────────────
    # Получение / создание записи суточных лимитов
    # ──────────────────────────────────────────────────────────────

    def _get_or_create_daily_limit(self) -> DailyLimit:
        """
        Возвращает запись DailyLimit для текущего аккаунта и сегодняшней даты.
        Если записи нет — создаёт новую с вычисленными лимитами.
        """
        today = date.today()
        record = (
            self._db.query(DailyLimit)
            .filter_by(account_id=self._account_id, date=today)
            .first()
        )

        if record is None:
            record = DailyLimit(
                account_id=self._account_id,
                date=today,
                likes_limit=self._limits.get("likes_per_day", 150),
                follows_limit=self._limits.get("follows_per_day", 30),
                unfollows_limit=self._limits.get("unfollows_per_day", 30),
                story_views_limit=self._limits.get("story_views_per_day", 60),
                comments_limit=self._limits.get("comments_per_day", 5),
                dm_limit=self._limits.get("dm_per_day", 0),
            )
            self._db.add(record)
            self._db.commit()
            self._log.info("daily_limit_created", date=str(today), limits=self._limits)

        return record

    # ──────────────────────────────────────────────────────────────
    # Проверка: можно ли выполнить действие?
    # ──────────────────────────────────────────────────────────────

    def can_act(self, action: ActionType) -> bool:
        """
        Проверяет, не превышен ли суточный лимит для данного типа действия.

        Args:
            action: Тип действия (LIKE, FOLLOW, STORY_VIEW и т.д.)

        Returns:
            True — действие разрешено, False — лимит исчерпан.
        """
        record = self._get_or_create_daily_limit()
        current = record.get_count(action)
        limit = record.get_limit(action)

        if current >= limit:
            self._log.warning(
                "limit_reached",
                action=action.value,
                current=current,
                limit=limit,
            )
            return False

        self._log.debug(
            "can_act_ok",
            action=action.value,
            current=current,
            limit=limit,
            remaining=limit - current,
        )
        return True

    # ──────────────────────────────────────────────────────────────
    # Задержка (имитация человека)
    # ──────────────────────────────────────────────────────────────

    async def wait(self) -> float:
        """
        Асинхронная пауза между действиями.

        Логика:
          1. Основная пауза: random(action_min, action_max) секунд.
          2. С вероятностью extra_delay_chance — дополнительная пауза
             (имитация «чтения профиля»).

        Returns:
            Суммарное время ожидания в секундах.
        """
        base_delay = random.uniform(
            self._delays["action_min"],
            self._delays["action_max"],
        )

        extra = 0.0
        if random.random() < self._delays["extra_delay_chance"]:
            extra = random.uniform(
                self._delays["extra_delay_min"],
                self._delays["extra_delay_max"],
            )
            self._log.debug("extra_delay_triggered", extra_seconds=round(extra, 1))

        total = base_delay + extra

        self._log.debug(
            "waiting",
            base=round(base_delay, 1),
            extra=round(extra, 1),
            total=round(total, 1),
        )
        await asyncio.sleep(total)
        return total

    def wait_sync(self) -> float:
        """
        Синхронная версия wait() для использования вне async-контекста.
        """
        import time

        base_delay = random.uniform(
            self._delays["action_min"],
            self._delays["action_max"],
        )
        extra = 0.0
        if random.random() < self._delays["extra_delay_chance"]:
            extra = random.uniform(
                self._delays["extra_delay_min"],
                self._delays["extra_delay_max"],
            )

        total = base_delay + extra
        time.sleep(total)
        return total

    # ──────────────────────────────────────────────────────────────
    # Запись действия в счётчики
    # ──────────────────────────────────────────────────────────────

    def record_action(self, action: ActionType) -> None:
        """
        Инкрементирует суточный счётчик после успешного действия.
        Вызывается из actions.py ТОЛЬКО при статусе SUCCESS.

        Args:
            action: Тип выполненного действия.
        """
        record = self._get_or_create_daily_limit()
        record.increment(action)
        self._db.commit()

        self._log.info(
            "action_recorded",
            action=action.value,
            new_count=record.get_count(action),
            limit=record.get_limit(action),
        )

    # ──────────────────────────────────────────────────────────────
    # Сброс суточных лимитов (вызывается планировщиком)
    # ──────────────────────────────────────────────────────────────

    def reset_daily(self) -> None:
        """
        Сбрасывает счётчики на сегодня (создаёт новую запись).
        Вызывается из scheduler.py в начале нового дня.

        Фактически — следующий вызов _get_or_create_daily_limit()
        создаст свежую запись для новой даты автоматически.
        Этот метод логирует факт сброса.
        """
        self._log.info(
            "daily_reset",
            date=str(date.today()),
            limits=self._limits,
        )
        # Пересчитываем лимиты (например, аккаунт стал старше)
        self._limits = self.compute_limits()

    # ──────────────────────────────────────────────────────────────
    # Утилиты
    # ──────────────────────────────────────────────────────────────

    def get_remaining(self, action: ActionType) -> int:
        """Возвращает количество оставшихся действий на сегодня."""
        record = self._get_or_create_daily_limit()
        return max(0, record.get_limit(action) - record.get_count(action))

    def get_daily_stats(self) -> dict[str, int]:
        """Возвращает полную статистику суточных счётчиков."""
        record = self._get_or_create_daily_limit()
        return {
            "likes": record.likes_count,
            "likes_limit": record.likes_limit,
            "follows": record.follows_count,
            "follows_limit": record.follows_limit,
            "unfollows": record.unfollows_count,
            "unfollows_limit": record.unfollows_limit,
            "story_views": record.story_views_count,
            "story_views_limit": record.story_views_limit,
            "comments": record.comments_count,
            "comments_limit": record.comments_limit,
            "date": str(record.date),
        }
