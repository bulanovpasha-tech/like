"""
core/anti_ban.py — обработка блокировок и ограничений Instagram.

Обрабатывает:
  - HTTP 429 (Too Many Requests)
  - ChallengeRequired (капча / подозрительная активность)
  - LoginRequired (сессия устарела)
  - FeedbackRequired (temporary block)
  - Сетевые ошибки

Принцип: при любой блокировке — немедленная остановка, лог, пауза 2–24ч.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, TypeVar

import structlog
import yaml

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _ig_exceptions():
    """Ленивый импорт исключений instagrapi — избегаем загрузки moviepy при старте."""
    from instagrapi.exceptions import (
        ChallengeRequired,
        FeedbackRequired,
        LoginRequired,
        PleaseWaitFewMinutes,
        RateLimitError,
        ClientError,
        ClientConnectionError,
        ClientJSONDecodeError,
        BadPassword,
    )
    return (
        ChallengeRequired, FeedbackRequired, LoginRequired,
        PleaseWaitFewMinutes, RateLimitError, ClientError,
        ClientConnectionError, ClientJSONDecodeError, BadPassword,
    )

from db.models import Account, AccountStatus

logger = structlog.get_logger(__name__)

T = TypeVar("T")


def _load_delays(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("delays", {})


class AntiBanHandler:
    """
    Централизованная обработка ошибок Instagram API.

    Использование:
        handler = AntiBanHandler(account_id=1, db=db)
        result = await handler.safe_call(client.media_like, media_id=media_id)
    """

    def __init__(
        self,
        account_id: int,
        db: "Session",
        config_path: str = "config.yaml",
    ) -> None:
        self._account_id = account_id
        self._db = db
        self._delays = _load_delays(config_path)
        self._log = logger.bind(account_id=account_id)

    # ──────────────────────────────────────────────────────────────
    # Главный метод: оборачивает вызов API в обработчик ошибок
    # ──────────────────────────────────────────────────────────────

    async def safe_call(
        self,
        func: Callable[..., T],
        *args,
        **kwargs,
    ) -> tuple[T | None, bool]:
        """
        Вызывает func(*args, **kwargs) с перехватом всех Instagram-ошибок.

        Returns:
            (result, success): result=None при ошибке, success=False при блокировке.

        Raises:
            Не бросает исключений — все обрабатываются внутри.
        """
        (
            ChallengeRequired, FeedbackRequired, LoginRequired,
            PleaseWaitFewMinutes, RateLimitError, ClientError,
            ClientConnectionError, ClientJSONDecodeError, BadPassword,
        ) = _ig_exceptions()

        try:
            result = func(*args, **kwargs)
            return result, True

        except ChallengeRequired as e:
            await self._handle_challenge(str(e))
            return None, False

        except RateLimitError as e:
            await self._handle_rate_limit(str(e))
            return None, False

        except PleaseWaitFewMinutes as e:
            await self._handle_rate_limit(str(e), short=True)
            return None, False

        except FeedbackRequired as e:
            await self._handle_feedback_required(str(e))
            return None, False

        except LoginRequired as e:
            await self._handle_login_required(str(e))
            return None, False

        except BadPassword as e:
            self._handle_bad_password(str(e))
            return None, False

        except ClientConnectionError as e:
            await self._handle_network_error(str(e))
            return None, False

        except ClientJSONDecodeError as e:
            self._log.warning("json_decode_error", error=str(e))
            await asyncio.sleep(random.uniform(30, 90))
            return None, False

        except ClientError as e:
            self._log.error("client_error", error=str(e))
            self._update_account_error(str(e))
            return None, False

        except Exception as e:
            self._log.exception("unexpected_error", error=str(e))
            self._update_account_error(str(e))
            return None, False

    # ──────────────────────────────────────────────────────────────
    # Обработчики конкретных ошибок
    # ──────────────────────────────────────────────────────────────

    async def _handle_challenge(self, error: str) -> None:
        """
        ChallengeRequired — Instagram требует подтверждения личности.
        Статус → CHALLENGE. Пауза до ручного решения.
        """
        self._log.critical(
            "challenge_required",
            error=error,
            action="account_paused_until_manual_resolution",
        )
        # TODO: отправить уведомление в Telegram
        self._update_account_status(AccountStatus.CHALLENGE, error)
        # Большая пауза — ждём ручного вмешательства
        pause_hours = random.uniform(
            self._delays.get("ban_pause_min_hours", 2),
            self._delays.get("ban_pause_max_hours", 24),
        )
        await asyncio.sleep(pause_hours * 3600)

    async def _handle_rate_limit(self, error: str, short: bool = False) -> None:
        """
        429 / PleaseWaitFewMinutes — превышение частоты запросов.
        Статус → PAUSED. Пауза 2–24ч.
        """
        if short:
            pause_seconds = random.uniform(180, 600)
            self._log.warning("please_wait_few_minutes", error=error, pause_s=pause_seconds)
            await asyncio.sleep(pause_seconds)
            return

        pause_hours = random.uniform(
            self._delays.get("ban_pause_min_hours", 2),
            self._delays.get("ban_pause_max_hours", 24),
        )
        paused_until = datetime.utcnow() + timedelta(hours=pause_hours)

        self._log.error(
            "rate_limited_429",
            error=error,
            pause_hours=round(pause_hours, 1),
            paused_until=paused_until.isoformat(),
        )
        # TODO: отправить уведомление в Telegram

        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if account:
            account.status = AccountStatus.PAUSED
            account.paused_until = paused_until
            account.last_error = error
            self._db.commit()

        await asyncio.sleep(pause_hours * 3600)

    async def _handle_feedback_required(self, error: str) -> None:
        """
        FeedbackRequired — временная блокировка действия (не аккаунта).
        Пауза 2–6ч.
        """
        pause_hours = random.uniform(2, 6)
        self._log.error(
            "feedback_required_block",
            error=error,
            pause_hours=round(pause_hours, 1),
        )
        # TODO: уведомление
        self._update_account_status(AccountStatus.PAUSED, error)
        await asyncio.sleep(pause_hours * 3600)

    async def _handle_login_required(self, error: str) -> None:
        """
        LoginRequired — сессия истекла. Нужно переавторизоваться.
        Статус → ERROR. Не пытаемся автоматически — безопаснее вручную.
        """
        self._log.error("login_required", error=error)
        self._update_account_status(AccountStatus.ERROR, f"LoginRequired: {error}")
        # TODO: триггер переавторизации через session.py
        await asyncio.sleep(300)

    def _handle_bad_password(self, error: str) -> None:
        """Неверный пароль — помечаем аккаунт как ERROR."""
        self._log.critical("bad_password", error=error)
        self._update_account_status(AccountStatus.ERROR, f"BadPassword: {error}")

    async def _handle_network_error(self, error: str) -> None:
        """Сетевая ошибка — короткая пауза, потом повтор."""
        pause = random.uniform(30, 120)
        self._log.warning("network_error", error=error, retry_in=pause)
        await asyncio.sleep(pause)

    # ──────────────────────────────────────────────────────────────
    # Вспомогательные методы
    # ──────────────────────────────────────────────────────────────

    def _update_account_status(
        self,
        status: AccountStatus,
        error: str | None = None,
    ) -> None:
        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if account:
            account.status = status
            account.last_error = error
            account.consecutive_errors += 1
            account.updated_at = datetime.utcnow()
            self._db.commit()

    def _update_account_error(self, error: str) -> None:
        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if account:
            account.consecutive_errors += 1
            account.last_error = error
            if account.consecutive_errors >= 5:
                account.status = AccountStatus.ERROR
            self._db.commit()

    def is_account_blocked(self) -> bool:
        """Проверяет, не заблокирован ли аккаунт прямо сейчас."""
        account = self._db.query(Account).filter_by(id=self._account_id).first()
        if not account:
            return True
        if account.status in (AccountStatus.BANNED, AccountStatus.CHALLENGE):
            return True
        if account.status == AccountStatus.PAUSED and account.paused_until:
            if datetime.utcnow() < account.paused_until:
                return True
            else:
                # Пауза истекла — возобновляем
                account.status = AccountStatus.ACTIVE
                account.consecutive_errors = 0
                self._db.commit()
        return False
