# Образ приложения в две стадии. Собирается из корня проекта:
#   docker build -f deploy/Dockerfile -t beauty-agent:dev .

# Обе стадии — из одного базового образа: .venv ссылается на Python по полному пути
ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------- 1. builder
# Poetry ставит зависимости и сам пакет в /app/.venv
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_NO_INTERACTION=1

RUN pip install "poetry==2.5.1"

WORKDIR /app

# Сначала только описание зависимостей: пока оно не менялось, слой берётся из кэша
COPY pyproject.toml poetry.lock poetry.toml ./
RUN poetry install --only main --no-root

# Потом код и сам пакет: этот слой пересобирается при каждой правке, но он быстрый
COPY src ./src
RUN poetry install --only main

# ---------------------------------------------------------------- 2. runtime
# Только Python, .venv и код: без Poetry, pip-кэша и инструментов сборки
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home app

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src
# Настройки миграций: alembic upgrade head запускается прямо в контейнере
COPY alembic.ini ./

# Дальше всё запускается от непривилегированного пользователя
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    --start-period=10s --start-interval=1s \
    CMD ["python", "-c", \
    "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health')"]

# Exec-форма: uvicorn — процесс с PID 1 и сам получает SIGTERM от docker stop
CMD ["uvicorn", "beauty_agent.api.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
