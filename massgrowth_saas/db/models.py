"""
db/models.py — SQLAlchemy ORM модели.

Таблицы:
  - Account     : Instagram-аккаунты под управлением
  - DailyLimit  : Суточные счётчики действий (сбрасываются по планировщику)
  - ActionLog   : Полный лог каждого действия (JSONL-эквивалент в БД)
  - Task        : Задачи на продвижение (старт, стоп, статус)
"""

from __future__ import annotations

import enum
from datetime import datetime, date

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.database import Base


# ──────────────────────────────────────────────────────────────────
# Перечисления
# ──────────────────────────────────────────────────────────────────

class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"          # Временно приостановлен
    BANNED = "banned"          # Получил блокировку
    CHALLENGE = "challenge"    # Challenge Required — нужно ручное решение
    ERROR = "error"            # Неизвестная ошибка


class ActionType(str, enum.Enum):
    LIKE = "like"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"
    STORY_VIEW = "story_view"
    COMMENT = "comment"        # TODO: модуль комментариев
    DM = "dm"                  # TODO: модуль автодиректа


class ActionStatus(str, enum.Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"        # Пропущен фильтром
    LIMIT_REACHED = "limit_reached"
    ERROR = "error"
    BANNED = "banned"
    CHALLENGE = "challenge"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


# ──────────────────────────────────────────────────────────────────
# Account
# ──────────────────────────────────────────────────────────────────

class Account(Base):
    """
    Instagram-аккаунт, добавленный в систему.
    Пароль хранится ТОЛЬКО при первичном добавлении для получения cookies.
    После авторизации credentials стираются, работаем только через сессию.
    """
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # Credentials (шифровать в продакшне через Fernet / HashiCorp Vault)
    # TODO: заменить на зашифрованное хранение через cryptography.fernet
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON cookies

    # Прокси (1:1 с аккаунтом)
    proxy: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Метаданные аккаунта
    ig_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_age_days: Mapped[int] = mapped_column(Integer, default=0)
    safety_mode: Mapped[str] = mapped_column(String(16), default="soft")

    # Статус
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.ACTIVE, index=True
    )
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)

    # Временные метки
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Отношения
    daily_limits: Mapped[list["DailyLimit"]] = relationship(
        "DailyLimit", back_populates="account", cascade="all, delete-orphan"
    )
    action_logs: Mapped[list["ActionLog"]] = relationship(
        "ActionLog", back_populates="account", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        "Task", back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account {self.username} [{self.status}]>"


# ──────────────────────────────────────────────────────────────────
# DailyLimit — суточные счётчики
# ──────────────────────────────────────────────────────────────────

class DailyLimit(Base):
    """
    Суточный счётчик действий для одного аккаунта.
    Одна запись на аккаунт × дату.
    Сбрасывается планировщиком в scheduler.py в 00:05 UTC.
    """
    __tablename__ = "daily_limits"
    __table_args__ = (
        UniqueConstraint("account_id", "date", name="uq_account_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(Date, default=date.today, index=True)

    # Счётчики (инкрементируются в safety.py)
    likes_count: Mapped[int] = mapped_column(Integer, default=0)
    follows_count: Mapped[int] = mapped_column(Integer, default=0)
    unfollows_count: Mapped[int] = mapped_column(Integer, default=0)
    story_views_count: Mapped[int] = mapped_column(Integer, default=0)
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    dm_count: Mapped[int] = mapped_column(Integer, default=0)

    # Максимальные лимиты на этот день (берутся из config + age_multiplier)
    likes_limit: Mapped[int] = mapped_column(Integer, default=150)
    follows_limit: Mapped[int] = mapped_column(Integer, default=30)
    unfollows_limit: Mapped[int] = mapped_column(Integer, default=30)
    story_views_limit: Mapped[int] = mapped_column(Integer, default=60)
    comments_limit: Mapped[int] = mapped_column(Integer, default=5)
    dm_limit: Mapped[int] = mapped_column(Integer, default=0)

    account: Mapped["Account"] = relationship("Account", back_populates="daily_limits")

    def get_count(self, action: ActionType) -> int:
        """Возвращает текущий счётчик для типа действия."""
        mapping = {
            ActionType.LIKE: self.likes_count,
            ActionType.FOLLOW: self.follows_count,
            ActionType.UNFOLLOW: self.unfollows_count,
            ActionType.STORY_VIEW: self.story_views_count,
            ActionType.COMMENT: self.comments_count,
            ActionType.DM: self.dm_count,
        }
        return mapping.get(action, 0)

    def get_limit(self, action: ActionType) -> int:
        """Возвращает лимит для типа действия."""
        mapping = {
            ActionType.LIKE: self.likes_limit,
            ActionType.FOLLOW: self.follows_limit,
            ActionType.UNFOLLOW: self.unfollows_limit,
            ActionType.STORY_VIEW: self.story_views_limit,
            ActionType.COMMENT: self.comments_limit,
            ActionType.DM: self.dm_limit,
        }
        return mapping.get(action, 0)

    def increment(self, action: ActionType) -> None:
        """Инкрементирует счётчик для типа действия."""
        attr_map = {
            ActionType.LIKE: "likes_count",
            ActionType.FOLLOW: "follows_count",
            ActionType.UNFOLLOW: "unfollows_count",
            ActionType.STORY_VIEW: "story_views_count",
            ActionType.COMMENT: "comments_count",
            ActionType.DM: "dm_count",
        }
        attr = attr_map.get(action)
        if attr:
            setattr(self, attr, getattr(self, attr) + 1)

    def __repr__(self) -> str:
        return f"<DailyLimit account_id={self.account_id} date={self.date}>"


# ──────────────────────────────────────────────────────────────────
# ActionLog — полный аудит действий
# ──────────────────────────────────────────────────────────────────

class ActionLog(Base):
    """
    Каждое действие автоматизации записывается сюда.
    Используется для статистики, дебага и аудита безопасности.
    """
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    # Что, с кем, когда
    action_type: Mapped[ActionType] = mapped_column(Enum(ActionType), index=True)
    target_username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Результат
    status: Mapped[ActionStatus] = mapped_column(
        Enum(ActionStatus), default=ActionStatus.SUCCESS, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Метрики
    delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    proxy_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Источник (откуда взят пользователь)
    source: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # "geo:Moscow" | "competitor:@username"

    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )

    account: Mapped["Account"] = relationship("Account", back_populates="action_logs")

    def __repr__(self) -> str:
        return (
            f"<ActionLog {self.action_type} → {self.target_username} "
            f"[{self.status}] @ {self.timestamp}>"
        )


# ──────────────────────────────────────────────────────────────────
# Task — задачи продвижения
# ──────────────────────────────────────────────────────────────────

class Task(Base):
    """
    Задача на продвижение: один аккаунт + параметры + статус.
    Управляется через API (POST /start, /stop) и планировщиком.
    """
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    # Параметры задачи (JSON-строка с настройками)
    config_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Статус
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Статистика сессии
    actions_done: Mapped[int] = mapped_column(Integer, default=0)
    likes_done: Mapped[int] = mapped_column(Integer, default=0)
    follows_done: Mapped[int] = mapped_column(Integer, default=0)
    story_views_done: Mapped[int] = mapped_column(Integer, default=0)

    # Временные метки
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped["Account"] = relationship("Account", back_populates="tasks")

    def __repr__(self) -> str:
        return f"<Task id={self.id} account_id={self.account_id} [{self.status}]>"
