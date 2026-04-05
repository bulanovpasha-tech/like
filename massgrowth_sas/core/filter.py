import instagrapi
import structlog
from typing import Dict, Any

logger = structlog.get_logger()

class AudienceFilter:
    """
    Фильтр аудитории для отбора целевых пользователей.
    Отсеивает ботов, бизнес-аккаунты и неактивные профили.
    """
    def __init__(self, config):
        self.min_followers = config['targeting']['min_followers']
        self.max_followers = config['targeting']['max_followers']
        self.exclude_business = config['targeting']['exclude_business']
        self.require_avatar = config['targeting']['require_avatar']

    def is_valid_target(self, user_info: Dict[str, Any]) -> bool:
        """
        Проверяет пользователя на соответствие критериям.
        user_info: dict от instagrapi.user_info
        """
        username = user_info.get('username', 'unknown')
        
        # 1. Проверка количества подписчиков
        follower_count = user_info.get('follower_count', 0)
        if not (self.min_followers <= follower_count <= self.max_followers):
            logger.debug("filter_followers_fail", username=username, count=follower_count)
            return False

        # 2. Исключение бизнес-аккаунтов
        if self.exclude_business and user_info.get('is_business', False):
            logger.debug("filter_business_fail", username=username)
            return False

        # 3. Проверка аватара
        if self.require_avatar and not user_info.get('profile_pic_url'):
            logger.debug("filter_avatar_fail", username=username)
            return False

        # 4. Простая эвристика на бота (слишком много подписок при мало фолловеров)
        following_count = user_info.get('following_count', 0)
        if follower_count > 0 and following_count / follower_count > 10:
             logger.debug("filter_bot_ratio_fail", username=username)
             return False

        # 5. Проверка наличия постов (пустой профиль)
        if user_info.get('media_count', 0) == 0:
            logger.debug("filter_empty_profile", username=username)
            return False

        logger.info("target_validated", username=username)
        return True
