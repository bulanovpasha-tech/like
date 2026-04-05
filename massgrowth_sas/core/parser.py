import instagrapi
import structlog
from typing import List, Optional
from core.safety import SafetyController

logger = structlog.get_logger()

class Parser:
    """
    Парсер для получения постов и пользователей по геолокации или хэштегам.
    """
    def __init__(self, client: instagrapi.Client, config):
        self.client = client
        self.config = config
        self.safety = SafetyController()

    async def get_medias_by_location(self, location_id: str, count: int = 20) -> List[str]:
        """
        Получает список media_id по идентификатору локации.
        Возвращает список ID медиа для последующей обработки.
        """
        try:
            # TODO: Добавить задержку перед запросом
            medias = self.client.location_medias_top(location_id, amount=count)
            return [media.id for media in medias]
        except Exception as e:
            logger.error("parse_location_failed", location_id=location_id, error=str(e))
            return []

    async def get_medias_by_hashtag(self, hashtag: str, count: int = 20) -> List[str]:
        """
        Получает список media_id по хэштегу.
        """
        try:
            # TODO: Добавить задержку перед запросом
            medias = self.client.hashtag_medias_top(name=hashtag, amount=count)
            return [media.id for media in medias]
        except Exception as e:
            logger.error("parse_hashtag_failed", hashtag=hashtag, error=str(e))
            return []

    async def get_competitor_followers(self, user_id: str, count: int = 50) -> List[str]:
        """
        Получает список user_id подписчиков конкурента.
        Внимание: Instagram сильно ограничивает этот эндпоинт.
        """
        try:
            # TODO: Реализовать с осторожностью, возможно потребуется Appium
            followers = self.client.user_followers(user_id=user_id, amount=count)
            return [str(f.id) for f in followers.values()]
        except Exception as e:
            logger.error("parse_followers_failed", user_id=user_id, error=str(e))
            return []

    async def get_media_owner(self, media_id: str) -> Optional[str]:
        """
        Получает user_id владельца медиа.
        """
        try:
            media_info = self.client.media_info(media_id)
            if media_info and hasattr(media_info, 'user'):
                return str(media_info.user.pk)
            return None
        except Exception as e:
            logger.error("get_media_owner_failed", media_id=media_id, error=str(e))
            return None
