"""
core/parser.py — получение целевой аудитории.

Два источника:
  1. Геолокация (location_id) — посты рядом с городом клиента.
  2. Подписчики конкурента (competitor_user_id).

Каждый метод возвращает генератор UserShort для экономии памяти.
Полная информация (User) загружается только для прошедших быстрый фильтр.

TODO: парсинг по хэштегам (ниша + город).
TODO: парсинг комментаторов конкурента.
TODO: кэширование результатов в Redis для сокращения API-запросов.
"""

from __future__ import annotations

import random
import time
from typing import Generator, TYPE_CHECKING

import structlog
import yaml
if TYPE_CHECKING:
    from instagrapi import Client
    from instagrapi.types import UserShort, User, Media

from core.filter import ProfileFilter

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


def _load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class AudienceParser:
    """
    Парсер целевой аудитории для Instagram.

    Args:
        client: Авторизованный instagrapi Client.
        profile_filter: Экземпляр ProfileFilter.
        config_path: Путь к config.yaml.
    """

    def __init__(
        self,
        client: Client,
        profile_filter: ProfileFilter,
        config_path: str = "config.yaml",
    ) -> None:
        self._client = client
        self._filter = profile_filter
        self._cfg = _load_config(config_path)
        self._log = logger.bind(component="AudienceParser")

    # ──────────────────────────────────────────────────────────────
    # Парсинг по геолокации
    # ──────────────────────────────────────────────────────────────

    def get_users_by_location(
        self,
        location_id: int,
        max_posts: int = 50,
    ) -> Generator[tuple[UserShort, str | None], None, None]:
        """
        Получает авторов постов из заданной геолокации.

        Args:
            location_id: Instagram location ID.
            max_posts: Максимальное количество постов для обработки.

        Yields:
            (UserShort, media_id) для каждого уникального пользователя.
        """
        self._log.info("parsing_by_location", location_id=location_id, max_posts=max_posts)

        try:
            medias: list[Media] = self._client.location_medias_recent(
                location_pk=location_id,
                amount=max_posts,
            )
        except ClientError as e:
            self._log.error("location_medias_error", location_id=location_id, error=str(e))
            return

        seen_user_ids: set[str] = set()

        for media in medias:
            if not media.user:
                continue

            user_short = media.user
            uid = str(user_short.pk)

            if uid in seen_user_ids:
                continue
            seen_user_ids.add(uid)

            if not self._filter.passes_short(user_short):
                continue

            # Пауза между обращениями к спискам
            time.sleep(random.uniform(1.5, 4.0))

            yield user_short, str(media.pk)

        self._log.info(
            "location_parse_done",
            location_id=location_id,
            total_medias=len(medias),
            unique_candidates=len(seen_user_ids),
        )

    # ──────────────────────────────────────────────────────────────
    # Парсинг подписчиков конкурента
    # ──────────────────────────────────────────────────────────────

    def get_followers_of_competitor(
        self,
        competitor_username: str,
        max_users: int = 200,
    ) -> Generator[UserShort, None, None]:
        """
        Получает подписчиков аккаунта конкурента.

        Args:
            competitor_username: @username конкурента (без @).
            max_users: Максимальное количество подписчиков.

        Yields:
            UserShort для каждого подписчика, прошедшего быстрый фильтр.
        """
        self._log.info(
            "parsing_competitor_followers",
            competitor=competitor_username,
            max_users=max_users,
        )

        try:
            competitor_id = self._client.user_id_from_username(competitor_username)
        except ClientError as e:
            self._log.error(
                "competitor_user_id_error",
                competitor=competitor_username,
                error=str(e),
            )
            return

        try:
            followers: list[UserShort] = self._client.user_followers(
                user_id=competitor_id,
                amount=max_users,
            )
        except ClientError as e:
            self._log.error(
                "competitor_followers_error",
                competitor=competitor_username,
                error=str(e),
            )
            return

        count = 0
        for user_short in followers:
            if not self._filter.passes_short(user_short):
                continue

            time.sleep(random.uniform(0.5, 2.0))
            yield user_short
            count += 1

        self._log.info(
            "competitor_parse_done",
            competitor=competitor_username,
            total_fetched=len(followers),
            passed_filter=count,
        )

    # ──────────────────────────────────────────────────────────────
    # Загрузка полного профиля (с кэшированием)
    # ──────────────────────────────────────────────────────────────

    def get_full_user_info(self, user_id: str) -> User | None:
        """
        Загружает полный профиль пользователя.
        Это «дорогой» запрос — вызывать только для прошедших быстрый фильтр.

        Args:
            user_id: IG user ID.

        Returns:
            Объект User или None при ошибке.
        """
        try:
            time.sleep(random.uniform(1.0, 3.0))  # Пауза перед запросом
            user_info = self._client.user_info(user_id)
            return user_info
        except ClientError as e:
            self._log.warning("user_info_error", user_id=user_id, error=str(e))
            return None

    # ──────────────────────────────────────────────────────────────
    # Получение сторис пользователя
    # ──────────────────────────────────────────────────────────────

    def get_user_story_pks(self, user_id: str) -> list[int]:
        """
        Получает список PK активных сторис пользователя.

        Args:
            user_id: IG user ID.

        Returns:
            Список story PK или пустой список.
        """
        try:
            stories = self._client.user_stories(user_id=user_id)
            pks = [int(s.pk) for s in stories if s.pk]
            self._log.debug("stories_fetched", user_id=user_id, count=len(pks))
            return pks
        except ClientError as e:
            self._log.warning("stories_fetch_error", user_id=user_id, error=str(e))
            return []
