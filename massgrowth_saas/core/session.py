"""
core/session.py — безопасная авторизация Instagram.

Принципы:
  1. При первом логине получаем cookies и сохраняем в БД.
  2. При следующих запусках — только загружаем cookies, НЕ логинимся заново.
  3. Прокси привязан к аккаунту 1:1.
  4. При ChallengeRequired — немедленная остановка + лог.
  5. Пароль шифруется через Fernet (core/crypto.py) и расшифровывается только
     в момент логина — в памяти не остаётся дольше необходимого.

TODO: добавить поддержку 2FA (TOTP через pyotp).
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import TYPE_CHECKING

import structlog
from core.crypto import decrypt_password as _fernet_decrypt

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from db.models import Account, AccountStatus

logger = structlog.get_logger(__name__)

# User-Agent пул — ротируем для снижения fingerprint
_USER_AGENTS = [
    "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2340; samsung; SM-G991B; o1s; exynos2100; en_US; 458227631)",
    "Instagram 269.0.0.18.75 Android (31/12; 480dpi; 1080x2400; OnePlus; IN2013; OnePlus8T; qcom; en_US; 438569829)",
    "Instagram 272.0.0.15.96 Android (32/12; 440dpi; 1080x2280; Xiaomi; M2012K11AG; alioth; qcom; en_US; 449468359)",
    "Instagram 271.0.0.16.96 Android (30/11; 560dpi; 1440x3200; samsung; SM-N986B; c2q; qcom; en_US; 447973396)",
]


def _build_proxy_dict(proxy_url: str | None) -> dict | None:
    """
    Формирует словарь прокси для instagrapi.

    Args:
        proxy_url: Строка вида "http://user:pass@host:port" или None.

    Returns:
        Словарь прокси или None если прокси не задан.
    """
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


class SessionManager:
    """
    Управляет жизненным циклом Instagram-сессии для одного аккаунта.

    Args:
        account_id: ID аккаунта в БД.
        db: SQLAlchemy-сессия.

    Usage:
        manager = SessionManager(account_id=1, db=db)
        client = await manager.get_client()
        # использовать client для API-вызовов
        await manager.save_session(client)
    """

    def __init__(self, account_id: int, db: "Session") -> None:
        self._account_id = account_id
        self._db = db
        self._log = logger.bind(account_id=account_id)
        self._client: Client | None = None

    # ──────────────────────────────────────────────────────────────
    # Публичный интерфейс
    # ──────────────────────────────────────────────────────────────

    def get_client(self) -> Client:
        """
        Возвращает авторизованный instagrapi Client.

        Алгоритм:
          1. Если сессия уже в памяти — вернуть.
          2. Попробовать загрузить из cookies (БД).
          3. Если cookies нет или устарели — логин по паролю (один раз).

        Returns:
            Авторизованный Client.

        Raises:
            RuntimeError: Если авторизация невозможна.
        """
        if self._client is not None:
            return self._client

        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if not account:
            raise RuntimeError(f"Account {self._account_id} not found in DB")

        client = self._create_base_client(account)

        # Попытка 1: загрузить сессию из cookies
        if account.session_data:
            if self._try_load_session(client, account):
                self._client = client
                return client
            self._log.warning("session_load_failed_will_relogin")

        # Попытка 2: логин по паролю
        if account.password_encrypted:
            password = self._decrypt_password(account.password_encrypted)
            self._login_with_password(client, account.username, password)
            self._save_session_to_db(client, account)
            # Стираем пароль из памяти (не из БД — нужен при ре-логине)
            del password
            self._client = client
            return client

        raise RuntimeError(
            f"No session and no password for account {account.username}. "
            "Please add credentials via API first."
        )

    def save_session(self) -> None:
        """Сохраняет текущее состояние сессии в БД (после успешных действий)."""
        if self._client is None:
            return
        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if account:
            self._save_session_to_db(self._client, account)

    def logout(self) -> None:
        """Безопасный выход из сессии."""
        if self._client:
            try:
                self._client.logout()
                self._log.info("logout_success")
            except Exception as e:
                self._log.warning("logout_error", error=str(e))
            finally:
                self._client = None

    # ──────────────────────────────────────────────────────────────
    # Внутренние методы
    # ──────────────────────────────────────────────────────────────

    def _create_base_client(self, account: Account) -> "Client":
        """
        Создаёт базовый Client с настройками прокси и User-Agent.
        НЕ авторизует.
        """
        from instagrapi import Client
        client = Client()

        # Прокси (1 прокси = 1 аккаунт)
        if account.proxy:
            proxy = _build_proxy_dict(account.proxy)
            client.set_proxy(account.proxy)
            self._log.debug("proxy_set", proxy=self._mask_proxy(account.proxy))
        else:
            self._log.warning("no_proxy_set", note="Running without proxy is not recommended")

        # Рандомный User-Agent из пула
        ua = random.choice(_USER_AGENTS)
        client.set_user_agent(ua)

        # Задержки между запросами внутри instagrapi
        client.delay_range = [1, 3]

        return client

    def _try_load_session(self, client: Client, account: Account) -> bool:
        """
        Пробует восстановить сессию из сохранённых cookies.

        Returns:
            True если сессия жива, False если истекла.
        """
        try:
            session_data = json.loads(account.session_data)
            client.set_settings(session_data)
            client.get_timeline_feed()  # Лёгкая проверка сессии

            self._log.info(
                "session_restored",
                username=account.username,
                ig_user_id=account.ig_user_id,
            )
            return True

        except Exception as e:
            if "LoginRequired" in type(e).__name__ or "login" in str(e).lower():
                self._log.warning("session_expired", username=account.username)
                return False
            raise

        except Exception as e:
            self._log.warning("session_check_failed", error=str(e))
            return False

    def _login_with_password(
        self, client: Client, username: str, password: str
    ) -> None:
        """
        Выполняет первичный логин. Добавляет человекоподобные задержки.

        Raises:
            RuntimeError: При ChallengeRequired, BadPassword и др.
        """
        # Задержка перед логином (имитация открытия приложения)
        time.sleep(random.uniform(3, 8))

        from instagrapi.exceptions import (
            BadPassword, ChallengeRequired, LoginRequired,
            TwoFactorRequired, ClientError,
        )

        self._log.info("login_attempt", username=username)
        try:
            client.login(username, password)
            self._log.info("login_success", username=username)

        except ChallengeRequired:
            self._log.critical(
                "login_challenge_required",
                username=username,
                action="manual_intervention_needed",
            )
            # TODO: уведомление в Telegram
            raise RuntimeError(
                f"ChallengeRequired for {username}. "
                "Please resolve challenge manually in Instagram app."
            )

        except TwoFactorRequired:
            # TODO: реализовать TOTP через pyotp
            raise RuntimeError(
                f"2FA required for {username}. "
                "Add TOTP secret to account settings."
            )

        except BadPassword:
            self._log.error("bad_password", username=username)
            # Обновляем статус в БД
            account = self._db.query(Account).filter_by(id=self._account_id).first()
            if account:
                account.status = AccountStatus.ERROR
                account.last_error = "BadPassword"
                self._db.commit()
            raise RuntimeError(f"Bad password for account {username}")

        except ClientError as e:
            self._log.error("login_client_error", username=username, error=str(e))
            raise RuntimeError(f"Login failed for {username}: {e}")

    def _save_session_to_db(self, client: Client, account: Account) -> None:
        """Сериализует cookies и сохраняет в поле session_data."""
        try:
            settings = client.get_settings()
            account.session_data = json.dumps(settings)
            account.ig_user_id = str(client.user_id) if client.user_id else None
            account.updated_at = __import__("datetime").datetime.utcnow()
            self._db.commit()
            self._log.debug("session_saved", username=account.username)
        except Exception as e:
            self._log.error("session_save_failed", error=str(e))

    @staticmethod
    def _decrypt_password(encrypted: str) -> str:
        """
        Расшифровывает пароль из БД через Fernet (core/crypto.py).
        Ключ берётся из SECRET_KEY в .env.
        """
        return _fernet_decrypt(encrypted)

    @staticmethod
    def _mask_proxy(proxy: str) -> str:
        """Маскирует пароль прокси для логов."""
        if "@" in proxy:
            parts = proxy.split("@")
            auth = parts[0].split("//")[-1]
            if ":" in auth:
                user = auth.split(":")[0]
                return proxy.replace(auth, f"{user}:***")
        return proxy
