from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime

class StartTaskRequest(BaseModel):
    account_id: int
    mode: str = "soft" # soft, strict

class StatusResponse(BaseModel):
    account_id: int
    username: str
    status: str
    today_stats: dict
    warning: str = "Автоматизация может вызвать ограничения. Используйте режим Soft."

class StatsResponse(BaseModel):
    total_accounts: int
    active_tasks: int
    global_stats: dict
