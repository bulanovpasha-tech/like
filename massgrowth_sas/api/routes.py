from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import get_db
from db.models import Account, DailyLimit
from api.schemas import StartTaskRequest, StatusResponse, StatsResponse
from datetime import datetime
import sqlalchemy
from sqlalchemy import select

router = APIRouter()

@router.post("/start", response_model=dict)
async def start_task(req: StartTaskRequest, db: AsyncSession = Depends(get_db)):
    # TODO: Интеграция с APScheduler для запуска задачи
    # Здесь мы просто меняем статус, реальный запуск в scheduler.py
    stmt = select(Account).where(Account.id == req.account_id)
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    account.status = "active"
    await db.commit()
    
    return {"message": "Task started", "warning": "Автоматизация может вызвать ограничения. Используйте режим Soft."}

@router.get("/status/{account_id}", response_model=StatusResponse)
async def get_status(account_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(Account).where(Account.id == account_id)
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
        
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Получаем статистику за сегодня
    limit_stmt = select(DailyLimit).where(DailyLimit.account_id == account_id, DailyLimit.date == today)
    limit_res = await db.execute(limit_stmt)
    limit_obj = limit_res.scalar_one_or_none()
    
    stats = {
        "likes": limit_obj.likes_count if limit_obj else 0,
        "follows": limit_obj.follows_count if limit_obj else 0
    }
    
    return StatusResponse(
        account_id=account.id,
        username=account.username,
        status=account.status,
        today_stats=stats
    )

@router.get("/stats", response_model=StatsResponse)
async def get_global_stats(db: AsyncSession = Depends(get_db)):
    # Упрощенная статистика
    count_stmt = select(sqlalchemy.func.count(Account.id))
    res = await db.execute(count_stmt)
    total = res.scalar()
    
    return StatsResponse(
        total_accounts=total,
        active_tasks=0, # TODO: Подсчет активных задач из scheduler
        global_stats={}
    )
