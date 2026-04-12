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

# Хранилище клиентов ожидающих challenge-кода (account_id → client)
# Живёт только в памяти текущего процесса — достаточно для одной машины
_challenge_clients: dict[int, object] = {}

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
            # Создаём свежий клиент — только с fingerprint, без протухших cookies
            client = self._create_base_client(account, device_only=True)

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

    def _create_base_client(self, account: Account, device_only: bool = False) -> "Client":
        """
        Создаёт базовый Client с настройками прокси и User-Agent.
        Если device_only=True — восстанавливает только fingerprint устройства (без cookies).
        НЕ авторизует.
        """
        from instagrapi import Client
        client = Client()

        # Восстанавливаем fingerprint устройства из сохранённых данных
        # Это гарантирует, что Instagram видит «знакомое» устройство
        if account.session_data:
            try:
                saved = json.loads(account.session_data)
                device_snapshot = {}
                for key in ("uuids", "device_settings", "user_agent"):
                    if key in saved:
                        device_snapshot[key] = saved[key]
                if device_snapshot:
                    client.set_settings(device_snapshot)
                    self._log.debug("device_fingerprint_restored")
            except Exception as e:
                self._log.warning("device_fingerprint_restore_failed", error=str(e))
        else:
            # Первый логин: выбираем рандомный UA и сохраним fingerprint после успеха
            ua = random.choice(_USER_AGENTS)
            client.set_user_agent(ua)

        # Прокси (1 прокси = 1 аккаунт)
        if account.proxy:
            client.set_proxy(account.proxy)
            self._log.debug("proxy_set", proxy=self._mask_proxy(account.proxy))
        else:
            self._log.warning("no_proxy_set", note="Running without proxy is not recommended")

        # Таймаут запросов — чтобы не зависать при недоступном прокси
        client.request_timeout = 30

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
            else:
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
            BadPassword, ChallengeRequired, LoginRequired, ClientError,
        )
        try:
            from instagrapi.exceptions import TwoFactorRequired as _TwoFactor
        except ImportError:
            _TwoFactor = None

        self._log.info("login_attempt", username=username)
        try:
            client.login(username, password)
            self._log.info("login_success", username=username)

        except Exception as e:
            exc_name = type(e).__name__
            exc_str = str(e).lower()

            if isinstance(e, ChallengeRequired):
                self._handle_challenge_required(client, username)

            if (_TwoFactor and isinstance(e, _TwoFactor)) or \
               "twofactor" in exc_name.lower() or "two_factor" in exc_name.lower() or \
               "2fa" in exc_str:
                raise RuntimeError(
                    f"2FA required for {username}. "
                    "Add TOTP secret to account settings."
                )

            if isinstance(e, BadPassword):
                # Проверяем: может это скрытый checkpoint (Instagram маскирует под bad_password)
                if self._maybe_challenge_in_response(client, username):
                    return  # challenge инициирован, ждём код

                ip_blocked = self._detect_ip_block(e)
                account = self._db.query(Account).filter_by(id=self._account_id).first()
                if account:
                    account.status = AccountStatus.ERROR
                    account.last_error = "IPBlocked" if ip_blocked else "BadPassword"
                    self._db.commit()
                if ip_blocked:
                    self._log.error("login_ip_blocked", username=username)
                    raise RuntimeError(
                        f"Instagram заблокировал вход с вашего IP для {username}. "
                        "Подождите 3-4 часа или используйте прокси."
                    )
                self._log.error("bad_password", username=username)
                raise RuntimeError(f"Bad password for account {username}")

            if isinstance(e, ClientError):
                self._log.error("login_client_error", username=username, error=str(e))
                raise RuntimeError(f"Login failed for {username}: {e}")

            self._log.error("login_unexpected_error", username=username, error=str(e))
            raise RuntimeError(f"Login failed for {username}: {e}")

    def _handle_challenge_required(self, client, username: str) -> None:
        """
        Обрабатывает ChallengeRequired: отправляет код на email и
        сохраняет клиент в памяти для последующего завершения логина.
        """
        self._log.warning("login_challenge_required", username=username)
        try:
            # Пытаемся запросить код по email (choice=1)
            challenge = getattr(client, "last_json", {}).get("challenge", {})
            if challenge:
                try:
                    client.challenge_resolve(client.last_json)
                except Exception:
                    pass
                try:
                    client.challenge_send_security_code(1)  # 1 = email
                except Exception:
                    pass
        except Exception as ex:
            self._log.debug("challenge_init_error", error=str(ex))

        # Сохраняем клиент для последующего завершения через API
        _challenge_clients[self._account_id] = client
        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if account:
            account.status = AccountStatus.CHALLENGE
            account.last_error = "ChallengeRequired"
            self._db.commit()
        raise RuntimeError(
            f"CHALLENGE_REQUIRED:{self._account_id}:"
            "Введите код из письма Instagram в дашборде."
        )

    def _maybe_challenge_in_response(self, client, username: str) -> bool:
        """
        Проверяет, содержит ли last_json от instagrapi данные checkpoint.
        Instagram иногда возвращает bad_password + отправляет email-код.
        """
        try:
            last = getattr(client, "last_json", {}) or {}
            has_challenge = (
                "challenge" in last or
                last.get("error_type") in ("checkpoint_required", "checkpoint_challenge_required") or
                "checkpoint" in str(last).lower()
            )
            if has_challenge:
                self._log.warning(
                    "hidden_challenge_detected",
                    username=username,
                    last_json_keys=list(last.keys()),
                )
                self._handle_challenge_required(client, username)
        except RuntimeError:
            raise
        except Exception:
            pass
        return False

    @classmethod
    def complete_challenge(
        cls,
        account_id: int,
        code: str,
        db: "Session",
    ) -> None:
        """
        Завершает логин после ввода пользователем кода из email.
        Вызывается из API endpoint POST /accounts/{id}/challenge.
        """
        client = _challenge_clients.get(account_id)
        if not client:
            raise RuntimeError("No pending challenge for this account. Try logging in again.")

        log = logger.bind(account_id=account_id)
        log.info("challenge_code_received", code_length=len(code))

        # Пробуем разные методы завершения challenge (зависит от версии instagrapi)
        completed = False
        for method_name in ("challenge_resolve_with_code", "challenge_verify", "challenge_send_code"):
            method = getattr(client, method_name, None)
            if method:
                try:
                    method(code)
                    completed = True
                    log.info("challenge_completed_via", method=method_name)
                    break
                except Exception as ex:
                    log.debug("challenge_method_failed", method=method_name, error=str(ex))

        # Обновляем состояние аккаунта
        account = db.query(Account).filter_by(id=account_id).first()
        if not account:
            raise RuntimeError("Account not found")

        if completed:
            try:
                # Проверяем сессию после challenge
                client.get_timeline_feed()
                settings = client.get_settings()
                account.session_data = json.dumps(settings)
                account.ig_user_id = str(client.user_id) if client.user_id else None
                account.status = AccountStatus.ACTIVE
                account.last_error = None
                db.commit()
                _challenge_clients.pop(account_id, None)
                log.info("challenge_login_success", username=account.username)
            except Exception as ex:
                log.error("challenge_session_verify_failed", error=str(ex))
                raise RuntimeError(f"Challenge completed but session invalid: {ex}")
        else:
            account.last_error = "ChallengeFailed"
            db.commit()
            raise RuntimeError("Could not complete challenge. Try again or re-add account.")

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

    def _detect_ip_block(self, exc: Exception) -> bool:
        """
        Пытается определить, является ли BadPassword блокировкой IP,
        а не реально неверным паролем.

        Признаки блокировки IP (не неверного пароля):
        - response содержит checkpoint_required, feedback_required
        - error_title / message намекает на подозрительную активность
        - многократные попытки с одного IP
        """
        try:
            response = getattr(exc, "response", None)
            if response is None:
                return False

            # Пробуем получить JSON из ответа
            try:
                body = response.json()
            except Exception:
                body = {}

            # Признаки блокировки IP
            ip_block_signals = [
                "checkpoint_required",
                "feedback_required",
                "suspicious",
                "unusual",
                "rate_limit",
                "too_many",
            ]

            body_str = str(body).lower()
            for signal in ip_block_signals:
                if signal in body_str:
                    return True

            # Если error_type явно не "bad_password" — скорее всего блокировка
            error_type = body.get("error_type", "")
            if error_type and error_type != "bad_password":
                return True

        except Exception as parse_err:
            self._log.debug("detect_ip_block_parse_error", error=str(parse_err))

        # Fallback: если уже была ошибка с тем же аккаунтом — это блокировка IP
        try:
            account = self._db.query(Account).filter_by(id=self._account_id).first()
            if account and account.last_error in ("BadPassword", "IPBlocked"):
                # Повторная ошибка — почти наверняка блокировка IP
                return True
        except Exception:
            pass

        return False

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
