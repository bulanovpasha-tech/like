import structlog
from datetime import datetime
from sqlalchemy.future import Session
from db.models import DailyLimit, ActionLog, Account
from core.safety import SafetyController
from core.session import SessionManager
import asyncio

logger = structlog.get_logger()

class ActionExecutor:
    """
    Выполнитель действий с проверкой лимитов и логированием.
    Все действия имеют случайные задержки для эмуляции человека.
    """
    def __init__(self, session_manager: SessionManager, db_session, account: Account):
        self.sm = session_manager
        self.db = db_session
        self.account = account
        self.safety = SafetyController()
        self.client = session_manager.get_client()

    async def _log_action(self, action_type: str, target: str, status: str, error: str = None, delay: float = 0):
        log_entry = ActionLog(
            account_id=self.account.id,
            action_type=action_type,
            target_username=target,
            target_id="", # TODO: заполнить ID
            status=status,
            error_message=error,
            delay_used=delay
        )
        self.db.add(log_entry)
        await self.db.commit()

    async def _update_limits(self, action_type: str):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        limit_obj = self.db.query(DailyLimit).filter_by(account_id=self.account.id, date=today).first()
        
        if not limit_obj:
            limit_obj = DailyLimit(account_id=self.account.id, date=today)
            self.db.add(limit_obj)
            await self.db.commit()
            self.db.refresh(limit_obj)

        if action_type == 'like':
            limit_obj.likes_count += 1
        elif action_type == 'follow':
            limit_obj.follows_count += 1
        elif action_type == 'story_view':
            limit_obj.story_views_count += 1
            
        await self.db.commit()

    async def safe_like(self, media_id: str, username: str):
        # Проверка лимита
        today = datetime.utcnow().strftime("%Y-%m-%d")
        limit_obj = self.db.query(DailyLimit).filter_by(account_id=self.account.id, date=today).first()
        current_likes = limit_obj.likes_count if limit_obj else 0
        
        if not self.safety.can_act(current_likes, 'likes'):
            logger.warning("action_skipped_limit", action="like", username=username)
            return False

        try:
            delay = self.safety.get_random_delay()
            logger.info("waiting_before_action", action="like", delay=delay)
            await asyncio.sleep(delay)
            
            self.client.media_like(media_id)
            
            await self._log_action("like", username, "success", delay=delay)
            await self._update_limits("like")
            logger.info("action_success", action="like", username=username)
            return True
            
        except Exception as e:
            logger.error("action_failed", action="like", username=username, error=str(e))
            await self._log_action("like", username, "failed", error=str(e))
            # TODO: Обработка 429 здесь (anti_ban)
            return False

    async def safe_follow(self, user_id: str, username: str):
        # Проверка лимита
        today = datetime.utcnow().strftime("%Y-%m-%d")
        limit_obj = self.db.query(DailyLimit).filter_by(account_id=self.account.id, date=today).first()
        current_follows = limit_obj.follows_count if limit_obj else 0
        
        if not self.safety.can_act(current_follows, 'follows'):
            return False

        try:
            delay = self.safety.get_random_delay()
            await asyncio.sleep(delay)
            
            self.client.user_follow(user_id)
            
            await self._log_action("follow", username, "success", delay=delay)
            await self._update_limits("follow")
            return True
        except Exception as e:
            await self._log_action("follow", username, "failed", error=str(e))
            return False

    async def safe_view_story(self, user_id: str, story_id: str, username: str):
        # Проверка лимита
        today = datetime.utcnow().strftime("%Y-%m-%d")
        limit_obj = self.db.query(DailyLimit).filter_by(account_id=self.account.id, date=today).first()
        current_views = limit_obj.story_views_count if limit_obj else 0
        
        if not self.safety.can_act(current_views, 'story_views'):
            return False

        try:
            delay = self.safety.get_random_delay()
            await asyncio.sleep(delay)
            
            # Instagrapi не имеет прямого метода view_story, но можно использовать internal API
            # TODO: Реализовать через client.internal_request для просмотра сторис
            # Для MVP пока заглушка
            logger.info("story_view_simulated", username=username, story_id=story_id)
            
            await self._log_action("story_view", username, "success", delay=delay)
            await self._update_limits("story_view")
            return True
        except Exception as e:
            await self._log_action("story_view", username, "failed", error=str(e))
            return False
