from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Enum, Float
from sqlalchemy.orm import relationship
from db.database import Base
from datetime import datetime
import enum

class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BANNED = "banned"
    CHALLENGE = "challenge_required"

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password = Column(String) # В продакшене шифровать!
    proxy = Column(String) # Format: user:pass@ip:port
    status = Column(String, default=AccountStatus.ACTIVE)
    last_activity = Column(DateTime, default=datetime.utcnow)
    
    limits = relationship("DailyLimit", back_populates="account", uselist=False)
    logs = relationship("ActionLog", back_populates="account")

class DailyLimit(Base):
    __tablename__ = "daily_limits"
    
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True)
    date = Column(String, index=True) # YYYY-MM-DD
    likes_count = Column(Integer, default=0)
    follows_count = Column(Integer, default=0)
    story_views_count = Column(Integer, default=0)
    
    account = relationship("Account", back_populates="limits")

class ActionLog(Base):
    __tablename__ = "action_logs"
    
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"))
    timestamp = Column(DateTime, default=datetime.utcnow)
    action_type = Column(String) # like, follow, view_story
    target_username = Column(String)
    target_id = Column(String)
    status = Column(String) # success, failed, skipped
    error_message = Column(String, nullable=True)
    delay_used = Column(Float)
    
    account = relationship("Account", back_populates="logs")
