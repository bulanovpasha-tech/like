import random
import time
import structlog
from datetime import datetime, timedelta
from typing import Optional
import yaml

logger = structlog.get_logger()

class SafetyController:
    """
    Контроллер безопасности для эмуляции человеческого поведения
    и соблюдения лимитов Instagram.
    """
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        self.limits = self.config['limits']
        
    def get_random_delay(self) -> float:
        """Генерирует случайную паузу между действиями."""
        base_min = self.limits['delays']['action_min']
        base_max = self.limits['delays']['action_max']
        delay = random.uniform(base_min, base_max)
        
        # Эмуляция "чтения" или раздумий
        if random.random() < self.limits['delays']['extra_delay_chance']:
            extra = self.limits['delays']['extra_delay_time']
            logger.info("extra_delay_triggered", extra_seconds=extra)
            delay += random.uniform(0, extra)
            
        return delay

    def can_act(self, current_count: int, action_type: str) -> bool:
        """Проверяет, не превышен ли дневной лимит."""
        limit_config = self.limits['daily'].get(action_type, {})
        if not limit_config:
            return False
            
        max_limit = limit_config.get('max', 0)
        if current_count >= max_limit:
            logger.warning("daily_limit_reached", action=action_type, count=current_count, limit=max_limit)
            return False
        return True

    def simulate_human_typing(self, length: int) -> float:
        """Возвращает время, которое заняло бы 'написание' комментария или поиска."""
        # ~0.5 сек на символ + рандом
        return length * 0.5 + random.uniform(1, 3)

    def reset_daily_check(self, last_reset: datetime) -> bool:
        """Проверяет, наступил ли новый день для сброса счетчиков."""
        now = datetime.utcnow()
        if last_reset.date() < now.date():
            return True
        return False
