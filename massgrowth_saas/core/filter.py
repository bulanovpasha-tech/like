"""
core/filter.py — фильтрация Instagram-профилей.

Отсеиваем:
  - Business-аккаунты и магазины
  - Ботов (< min_followers фолловеров или > max_followers)
  - Пустые профили (нет аватара, нет постов)
  - Приватные аккаунты (по умолчанию)
  - Аккаунты без недавних сторис (если require_recent_story=True)
  - Уже отфильтрованных / уже обработанных (дедупликация)

TODO: добавить NLP-фильтр по ключевым словам в bio.
TODO: добавить проверку геолокации аккаунта (bio + посты).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
import yaml
from instagrapi.types import UserShort, User

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


def _load_filter_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("audience_filter", {})


class ProfileFilter:
    """
    Фильтр профилей по критериям из config.yaml.

    Args:
        config_path: Путь к config.yaml.
        seen_user_ids: Множество уже обработанных user_id (дедупликация в рамках сессии).
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        seen_user_ids: set[str] | None = None,
    ) -> None:
        self._cfg = _load_filter_config(config_path)
        self._seen: set[str] = seen_user_ids or set()
        self._log = logger.bind(component="ProfileFilter")

        # Кэшируем параметры фильтра
        self._min_followers: int = self._cfg.get("min_followers", 50)
        self._max_followers: int = self._cfg.get("max_followers", 3000)
        self._require_avatar: bool = self._cfg.get("require_avatar", True)
        self._require_bio: bool = self._cfg.get("require_bio", False)
        self._require_recent_story: bool = self._cfg.get("require_recent_story", True)
        self._recent_story_days: int = self._cfg.get("recent_story_days", 3)
        self._exclude_business: bool = self._cfg.get("exclude_business", True)
        self._exclude_verified: bool = self._cfg.get("exclude_verified", False)
        self._exclude_private: bool = self._cfg.get("exclude_private", True)
        self._min_posts: int = self._cfg.get("min_posts", 3)

    # ──────────────────────────────────────────────────────────────
    # Главный метод
    # ──────────────────────────────────────────────────────────────

    def passes(self, user: User, skip_reason_log: bool = False) -> bool:
        """
        Проверяет, проходит ли профиль все фильтры.

        Args:
            user: Полный объект User из instagrapi.
            skip_reason_log: Если True — не логируем причину отсева (для batch).

        Returns:
            True — профиль подходит, False — отсеять.
        """
        checks = [
            (self._check_already_seen, "already_seen"),
            (self._check_private, "private_account"),
            (self._check_verified, "verified_account"),
            (self._check_business, "business_account"),
            (self._check_followers_range, "followers_out_of_range"),
            (self._check_min_posts, "too_few_posts"),
            (self._check_avatar, "no_avatar"),
            (self._check_bio, "no_bio"),
        ]

        for check_fn, reason in checks:
            if not check_fn(user):
                if not skip_reason_log:
                    self._log.debug(
                        "profile_rejected",
                        username=user.username,
                        reason=reason,
                        followers=user.follower_count,
                    )
                return False

        # Добавляем в seen после прохождения всех фильтров
        self._seen.add(str(user.pk))
        self._log.debug("profile_accepted", username=user.username)
        return True

    def passes_short(self, user: UserShort) -> bool:
        """
        Быстрая проверка по неполным данным (UserShort из списков).
        Для полной проверки нужен user_info() — дорогой запрос.

        Проверяет только то, что доступно в UserShort:
          - дедупликация
          - is_private (если есть)
          - is_verified
        """
        if str(user.pk) in self._seen:
            return False
        if self._exclude_private and getattr(user, "is_private", False):
            return False
        if self._exclude_verified and getattr(user, "is_verified", False):
            return False
        return True

    # ──────────────────────────────────────────────────────────────
    # Проверки
    # ──────────────────────────────────────────────────────────────

    def _check_already_seen(self, user: User) -> bool:
        """True = профиль НЕ был обработан."""
        return str(user.pk) not in self._seen

    def _check_private(self, user: User) -> bool:
        """True = профиль публичный (или исключение не нужно)."""
        if self._exclude_private and user.is_private:
            return False
        return True

    def _check_verified(self, user: User) -> bool:
        """True = профиль не верифицирован (или верифицированные разрешены)."""
        if self._exclude_verified and user.is_verified:
            return False
        return True

    def _check_business(self, user: User) -> bool:
        """
        True = профиль не является бизнес-аккаунтом.
        Проверяет account_type и наличие business-категории.
        """
        if not self._exclude_business:
            return True

        # account_type: 1=personal, 2=creator, 3=business
        account_type = getattr(user, "account_type", None)
        if account_type == 3:
            return False

        # Дополнительная проверка по category_name
        category = getattr(user, "category_name", "") or ""
        business_keywords = [
            "магазин", "store", "shop", "brand", "бренд",
            "official", "official account", "company", "компания",
        ]
        if any(kw.lower() in category.lower() for kw in business_keywords):
            return False

        return True

    def _check_followers_range(self, user: User) -> bool:
        """True = количество фолловеров в допустимом диапазоне."""
        fc = user.follower_count or 0
        return self._min_followers <= fc <= self._max_followers

    def _check_min_posts(self, user: User) -> bool:
        """True = количество постов не меньше минимального."""
        media_count = getattr(user, "media_count", 0) or 0
        return media_count >= self._min_posts

    def _check_avatar(self, user: User) -> bool:
        """True = у профиля есть аватар."""
        if not self._require_avatar:
            return True
        profile_pic_url = getattr(user, "profile_pic_url", None)
        return bool(profile_pic_url)

    def _check_bio(self, user: User) -> bool:
        """True = в профиле есть описание (если требуется)."""
        if not self._require_bio:
            return True
        bio = getattr(user, "biography", "") or ""
        return len(bio.strip()) > 0

    # ──────────────────────────────────────────────────────────────
    # TODO: проверка сторис (требует отдельного API-запроса)
    # ──────────────────────────────────────────────────────────────

    def check_has_recent_stories(
        self,
        user_id: str,
        story_timestamps: list[datetime] | None,
    ) -> bool:
        """
        Проверяет наличие недавних сторис.

        Вызывается ОТДЕЛЬНО от passes() т.к. требует API-запроса.
        story_timestamps передаётся из parser.py (уже загруженные данные).

        Args:
            user_id: Instagram user ID.
            story_timestamps: Список timestamp'ов сторис пользователя.

        Returns:
            True = есть сторис за последние recent_story_days дней.
        """
        if not self._require_recent_story:
            return True

        if not story_timestamps:
            self._log.debug("no_stories", user_id=user_id)
            return False

        now = datetime.now(tz=timezone.utc)
        cutoff = self._recent_story_days * 86400  # дни в секунды

        for ts in story_timestamps:
            ts_aware = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            age_seconds = (now - ts_aware).total_seconds()
            if age_seconds <= cutoff:
                return True

        self._log.debug(
            "stories_too_old",
            user_id=user_id,
            required_days=self._recent_story_days,
        )
        return False

    # ──────────────────────────────────────────────────────────────
    # Утилиты
    # ──────────────────────────────────────────────────────────────

    def add_to_seen(self, user_id: str) -> None:
        """Добавляет user_id в seen вручную (из БД при старте)."""
        self._seen.add(str(user_id))

    def seen_count(self) -> int:
        """Возвращает количество обработанных профилей в сессии."""
        return len(self._seen)

    @property
    def config(self) -> dict:
        """Возвращает текущие настройки фильтра."""
        return {
            "min_followers": self._min_followers,
            "max_followers": self._max_followers,
            "require_avatar": self._require_avatar,
            "require_bio": self._require_bio,
            "require_recent_story": self._require_recent_story,
            "recent_story_days": self._recent_story_days,
            "exclude_business": self._exclude_business,
            "exclude_verified": self._exclude_verified,
            "exclude_private": self._exclude_private,
            "min_posts": self._min_posts,
        }
