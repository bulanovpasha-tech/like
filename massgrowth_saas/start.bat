@echo off
chcp 65001 >nul
title MassGrowth SaaS

echo.
echo  ============================================
echo   MassGrowth SaaS — Запуск
echo  ============================================
echo.

:: Проверяем Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ОШИБКА] Python не найден!
    echo  Скачай с python.org/downloads
    echo  При установке выбери "Add Python to PATH"
    pause
    exit /b 1
)

echo  [OK] Python найден
python --version

:: Устанавливаем зависимости (только первый раз)
if not exist ".venv" (
    echo.
    echo  Создаю виртуальное окружение...
    python -m venv .venv
)

echo  Активирую окружение...
call .venv\Scripts\activate.bat

echo  Устанавливаю зависимости (первый раз ~2 минуты)...
pip install -r requirements.txt -q

echo.
echo  ============================================
echo   Сервер запущен!
echo   Открой браузер: http://localhost:8000
echo  ============================================
echo.

:: Открываем браузер автоматически
start "" "http://localhost:8000"

:: Запускаем сервер
python main.py

pause
