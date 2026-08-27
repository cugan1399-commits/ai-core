"""
Центральная конфигурация проекта.
Все значения читаются из переменных окружения — ничего не хардкодим.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- База данных ---
DATABASE_URL = os.environ["DATABASE_URL"]  # например: postgresql+asyncpg://user:pass@host/db

# --- Redis / Celery ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
# Режим для бесплатного тарифа без отдельного Background Worker: задачи выполняются
# синхронно прямо внутри веб-процесса, без реальной очереди/брокера. Осознанный
# временный компромисс — см. README, раздел "Free-тариф без воркера".
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "False").lower() == "true"

# --- Bitrix24: одно маркетплейс-приложение на всех клиентов ---
BITRIX_CLIENT_ID = os.environ["BITRIX_CLIENT_ID"]
BITRIX_CLIENT_SECRET = os.environ["BITRIX_CLIENT_SECRET"]
BITRIX_OAUTH_TOKEN_URL = "https://oauth.bitrix.info/oauth/token"

# --- AI-агент (модуль seller): эмбеддинги + LLM ---
# Локальная многоязычная модель эмбеддингов (поддерживает русский), без внешнего API —
# осознанный выбор ради экономии: не платить за embedding-запрос на каждый чанк каталога
# у каждого клиента. Работает на CPU.
EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384  # размерность вектора именно этой модели

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # опционален: нужен только модулю 'seller'
ANTHROPIC_MODEL = "claude-sonnet-5"

RAG_TOP_K = 5  # сколько похожих чанков подавать в промпт на один вопрос
ESCALATION_MARKER = "ESCALATE_TO_OPERATOR"  # структурированный сигнал вместо парсинга текста ответа

# --- Модули (фичи) ---
# Единственный источник правды о том, какие модули вообще существуют.
# enabled_modules клиента валидируется против этого множества —
# опечатка в названии модуля никогда не пройдёт молча.
ALLOWED_MODULES = {
    "testing",  # обучение/аттестация менеджеров через Открытые линии
    "seller",   # заглушка на будущее: разговоры с клиентами, каталог товаров
}

# --- Прочее ---
WEBHOOK_TIMEOUT_SECONDS = 5  # сколько мы даём себе на быстрый ответ 200 OK Битриксу


# --- Telegram (модуль seller) ---
# Публичный домен приложения — нужен, чтобы сформировать URL вебхука при
# подключении нового Telegram-бота (PUBLIC_BASE_URL + "/telegram/webhook/{token}").

PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]  # например: https://your-app.example.com
ADMIN_API_KEY = os.environ["ADMIN_API_KEY"]  # защита /telegram/admin/*