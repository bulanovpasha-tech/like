# =============================================================
# MassGrowth SaaS — Dockerfile
# Базовый образ: python:3.11-slim (стабильнее, чем 3.12 для instagrapi)
# =============================================================

FROM python:3.11-slim

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости отдельно для кэширования слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Создаём директории для данных и логов
RUN mkdir -p /app/data /app/logs

# Запускаем от непривилегированного пользователя
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "main.py"]
