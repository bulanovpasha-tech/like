# MassGrowth SaaS - Инструкция по запуску и тестированию

## 📋 Структура проекта

```
massgrowth_sas/
├── core/
│   ├── __init__.py
│   ├── session.py          # Авторизация, прокси, проверка сессии
│   ├── parser.py           # Получение постов по геолокации/хэштегам
│   ├── filter.py           # Фильтр аудитории (город, фолловеры, business)
│   ├── actions.py          # Лайк, просмотр сторис, подписка
│   └── safety.py           # Рандомизация пауз, эмуляция человека
├── api/
│   ├── __init__.py
│   ├── routes.py           # FastAPI endpoints
│   └── schemas.py          # Pydantic схемы
├── db/
│   ├── __init__.py
│   ├── models.py           # SQLAlchemy модели
│   └── database.py         # Настройки БД
├── logs/                   # Логи приложения
├── data/                   # SQLite база данных
├── .env                    # Переменные окружения
├── config.yaml             # Конфигурация лимитов и настроек
├── docker-compose.yml      # Docker конфигурация
├── Dockerfile
├── requirements.txt
├── scheduler.py            # APScheduler планировщик
└── main.py                 # Точка входа FastAPI
```

## 🚀 Быстрый старт

### Вариант 1: Локальный запуск (рекомендуется для разработки)

1. **Установка зависимостей:**
```bash
cd massgrowth_sas
pip install -r requirements.txt
```

2. **Настройка окружения:**
```bash
cp .env.example .env
# Отредактируйте .env при необходимости
```

3. **Запуск сервера:**
```bash
python main.py
```
Сервер запустится на `http://localhost:8000`

4. **Проверка работы:**
- Откройте `http://localhost:8000/docs` для Swagger UI
- Или используйте curl:
```bash
curl http://localhost:8000/api/stats
```

### Вариант 2: Docker Compose (продакшен)

```bash
docker compose up --build
```

Сервис будет доступен на `http://localhost:8000`

## 🧪 Тестирование безопасности

### 1. Проверка SafetyController

```bash
cd massgrowth_sas
python -c "
from core.safety import SafetyController

safety = SafetyController()
print(f'Random delay: {safety.get_random_delay():.2f} sec')
print(f'Can act (likes, 0): {safety.can_act(0, \"likes\")}')
print(f'Can act (likes, 200): {safety.can_act(200, \"likes\")}')
"
```

**Ожидаемый результат:**
- Задержка между 18-90 секундами (+30% шанс доп. задержки)
- `can_act(0, "likes")` → `True`
- `can_act(200, "likes")` → `False` (лимит 150 превышен)

### 2. Проверка фильтра аудитории

```bash
python -c "
from core.filter import AudienceFilter
import yaml

with open('config.yaml') as f:
    config = yaml.safe_load(f)

filter = AudienceFilter(config)

# Валидный пользователь
valid_user = {
    'username': 'beauty_master',
    'follower_count': 1500,
    'following_count': 800,
    'is_business': False,
    'profile_pic_url': 'http://example.com/pic.jpg',
    'media_count': 25
}

# Бизнес-аккаунт (должен быть отфильтрован)
business_user = {
    'username': 'shop_cosmetics',
    'follower_count': 1500,
    'is_business': True,
    'profile_pic_url': 'http://example.com/pic.jpg',
    'media_count': 100
}

print(f'Valid user: {filter.is_valid_target(valid_user)}')
print(f'Business user: {filter.is_valid_target(business_user)}')
"
```

### 3. Тестирование API

```bash
# Получить статистику
curl http://localhost:8000/api/stats

# Запустить задачу (предварительно создав аккаунт в БД)
curl -X POST http://localhost:8000/api/start \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1}'

# Получить статус аккаунта
curl http://localhost:8000/api/status/1
```

## ⚙️ Настройка лимитов

Откройте `config.yaml` и измените параметры:

```yaml
limits:
  daily:
    likes: 
      min: 50
      max: 150  # Уменьшите для новых аккаунтов
    follows:
      min: 10
      max: 30
  delays:
    action_min: 18  # Минимальная пауза (сек)
    action_max: 90  # Максимальная пауза (сек)
    extra_delay_chance: 0.3  # 30% шанс доп. задержки
```

**Рекомендации по режимам:**
- **Soft**: likes=50-100, follows=10-20 (для новых аккаунтов < 1 месяца)
- **Strict**: likes=100-200, follows=20-30 (аккаунты 1-6 месяцев)
- **Aggressive**: НЕ рекомендуется (риск бана > 80%)

## 🔐 Безопасность

### Принцип работы защиты:

1. **Рандомизация действий**: Каждое действие имеет случайную задержку 18-90 сек
2. **Эмуляция чтения**: 30% шанс дополнительной задержки 0-60 сек
3. **Дневные лимиты**: Строгий учёт всех действий
4. **Обработка ошибок**: При 429/Challenge аккаунт блокируется на 24ч

### Логирование:

Все действия записываются в JSONL формат в `logs/`:
```json
{"timestamp": "2024-01-01T12:00:00", "action": "like", "username": "target_user", "status": "success", "delay": 45.2}
```

## 📡 API Endpoints

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | `/` | Статус сервиса |
| GET | `/api/stats` | Общая статистика |
| POST | `/api/start` | Запуск задачи для аккаунта |
| GET | `/api/status/{id}` | Статус конкретного аккаунта |

**Все ответы содержат предупреждение:**
> "Автоматизация может вызвать ограничения. Используйте режим Soft."

## 🔧 Добавление аккаунта

Для добавления аккаунта через Python:

```python
import asyncio
from db.database import AsyncSessionLocal
from db.models import Account

async def add_account():
    async with AsyncSessionLocal() as session:
        account = Account(
            username='your_instagram_username',
            password='your_password',
            proxy='user:pass@ip:port',  # Опционально
            status='active'
        )
        session.add(account)
        await session.commit()
        print(f'Account ID: {account.id}')

asyncio.run(add_account())
```

## ⚠️ Важные предупреждения

1. **Instagram активно борется с автоматизацией**. Даже при соблюдении всех мер безопасности риск бана существует.
2. **Используйте только старые аккаунты** (возраст > 3 месяцев) для автоматизации.
3. **Никогда не используйте один прокси для нескольких аккаунтов**.
4. **При получении Challenge Required немедленно остановите аккаунт** и пройдите верификацию вручную.
5. **Этот код — MVP**. Для продакшена требуется доработка системы обработки ошибок и мониторинга.

## 🛠 TODO для будущих версий

- [ ] Интеграция с Appium для большей безопасности (эмуляция реального устройства)
- [ ] Система уведомлений (Telegram/email) при ошибках
- [ ] Автоматическое решение Challenge (через email/SMS)
- [ ] Поддержка нескольких потоков на аккаунт
- [ ] CRM интеграция для управления клиентами
- [ ] Генерация контента через AI
- [ ] Расширенная аналитика и отчёты

## 📞 Поддержка

При возникновении проблем:
1. Проверьте логи в `logs/server.log`
2. Убедитесь, что прокси работает корректно
3. Проверьте статус аккаунта в Instagram вручную
4. Уменьшите лимиты в `config.yaml`

---
**Помните**: Безопасность аккаунтов важнее скорости роста!
