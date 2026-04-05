import instagrapi
import structlog
from typing import Optional
from core.safety import SafetyController
from db.models import AccountStatus
import json

logger = structlog.get_logger()

class SessionManager:
    """
    Менеджер сессий Instagram.
    Обрабатывает авторизацию, прокси и защиту от Challenge Required.
    """
    def __init__(self, account_model, db_session):
        self.account_model = account_model
        self.db_session = db_session
        self.client = instagrapi.Client()
        self.safety = SafetyController()
        self.is_logged_in = False
        
    def setup_proxy(self, proxy_str: str):
        """Настройка прокси. Формат: http://user:pass@ip:port"""
        try:
            # Instagrapi принимает прокси как строку или dict
            self.client.set_proxy(proxy_str)
            logger.info("proxy_set", proxy=proxy_str.split('@')[-1]) # Скрываем креды в логе
        except Exception as e:
            logger.error("proxy_setup_failed", error=str(e))
            raise

    def login(self, username: str, password: str):
        """Безопасный вход с обработкой Challenge."""
        try:
            # Попытка загрузки настроек устройства, чтобы не палиться как новый девайс
            # TODO: Реализовать персистентное хранение device_settings JSON
            
            self.client.login(username, password)
            self.is_logged_in = True
            logger.info("login_success", username=username)
            
            # Обновляем статус в БД
            self.account_model.status = AccountStatus.ACTIVE
            return True
            
        except instagrapi.exceptions.ChallengeRequired as e:
            logger.error("challenge_required", username=username, details=str(e))
            self.account_model.status = AccountStatus.CHALLENGE
            # TODO: Отправить уведомление админу/пользователю
            return False
        except instagrapi.exceptions.LoginRequired:
            logger.error("login_failed", username=username)
            return False
        except Exception as e:
            logger.error("login_unknown_error", username=username, error=str(e))
            # При частых ошибках сети можно забанить аккаунт временно
            if "429" in str(e):
                self.account_model.status = AccountStatus.BANNED
            return False

    def get_client(self) -> Optional[instagrapi.Client]:
        if not self.is_logged_in:
            return None
        return self.client
