"""
app.py — точка входа для Vercel serverless.
Vercel ищет объект `app` (ASGI) в этом файле.
"""

from __future__ import annotations

import importlib
import os
import sys

# Абсолютный путь к massgrowth_saas — вставляем первым,
# чтобы избежать конфликта с Vercel-специфичной директорией api/
_ROOT = os.path.dirname(os.path.abspath(__file__))
_SAAS = os.path.join(_ROOT, "massgrowth_saas")
if _SAAS not in sys.path:
    sys.path.insert(0, _SAAS)

# SQLite на Vercel — только /tmp доступен для записи
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/massgrowth.db")

# Дефолтный ключ — ОБЯЗАТЕЛЬНО переопределить в Vercel Dashboard → Environment Variables
os.environ.setdefault("SECRET_KEY", "vercel-demo-key-CHANGE-IN-DASHBOARD")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("ALLOWED_ORIGINS", "*")

# Загружаем .env (только при локальной разработке)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Явный импорт через importlib, чтобы избежать конфликта имён модуля `api`
_main_spec = importlib.util.spec_from_file_location(
    "massgrowth_api",
    os.path.join(_SAAS, "api", "main.py"),
)
_main_mod = importlib.util.module_from_spec(_main_spec)
sys.modules["massgrowth_api"] = _main_mod
_main_spec.loader.exec_module(_main_mod)

# Создаём таблицы БД при cold start
from db.database import create_tables  # noqa: E402
create_tables()

# Объект app — Vercel ASGI handler
app = _main_mod.app
