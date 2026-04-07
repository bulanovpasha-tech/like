"""
app.py — точка входа для Vercel serverless.

Vercel импортирует объект `app` из этого файла и обслуживает HTTP-запросы.

Ограничения Vercel (serverless):
  - Нет постоянных фоновых процессов → APScheduler не запускается.
  - SQLite хранится в /tmp (эфемерный, сбрасывается при cold start).
  - Таймаут функции: 10 сек (hobby) / 60 сек (pro).
  - Promotion loop (long-running) запускается через /run-task endpoint
    который триггерится Vercel Cron (vercel.json → crons).

Переменные окружения — добавить в Vercel Dashboard → Settings → Environment Variables:
  SECRET_KEY      (обязательно, минимум 16 символов)
  ALLOWED_ORIGINS (например: https://your-frontend.vercel.app)
  LOG_LEVEL       (INFO)
"""

from __future__ import annotations

import os
import sys

# Добавляем massgrowth_saas в Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "massgrowth_saas"))

# SQLite на Vercel: только /tmp доступен для записи
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/massgrowth.db")

# Дефолтный SECRET_KEY для демо — ОБЯЗАТЕЛЬНО переопределить в Vercel
os.environ.setdefault("SECRET_KEY", "vercel-demo-key-CHANGE-IN-DASHBOARD")

# Загружаем .env (если есть — локальная разработка)
from dotenv import load_dotenv
load_dotenv()

# Создаём таблицы при cold start
from db.database import create_tables
create_tables()

# Импортируем FastAPI app — Vercel ищет именно объект `app`
from api.main import app  # noqa: F401 — экспортируется для Vercel
