# Один образ используется тремя сервисами на Render (командой запуска они отличаются):
#   - веб:    uvicorn main:app --host 0.0.0.0 --port $PORT
#   - воркер: celery -A core.queue.celery_app worker --loglevel=info
#   - beat:   celery -A core.queue.celery_app beat --loglevel=info

FROM python:3.12-slim

WORKDIR /app

# Системные зависимости нужны для сборки некоторых колёс (torch/sentence-transformers
# на некоторых платформах требует компилятор), плюс libpq для asyncpg.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Модель эмбеддингов скачивается при первом обращении (core/embeddings.py, lru_cache).
# Осознанно НЕ качаем её на этапе сборки образа — она нужна только сервисам,
# которые реально обрабатывают seller-модуль (веб и воркер), не нужна beat-сервису.
# Если время первого ответа станет проблемой — можно добавить прогрев модели сюда.

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
