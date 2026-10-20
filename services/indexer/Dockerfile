# Образ индексатора. Собирается из корня проекта:
#   docker build -f services/indexer/Dockerfile -t beauty-indexer:dev .
# Зависимости — из того же poetry.lock: индексатор пользуется общей библиотекой beauty_agent.rag
# (обход сайта, нарезка, эмбеддинги) — ADR 014. Тесты в образ не попадают.

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_NO_INTERACTION=1

RUN pip install "poetry==2.5.1"

WORKDIR /app
COPY pyproject.toml poetry.lock poetry.toml ./
RUN poetry install --only main --no-root
COPY src ./src
RUN poetry install --only main

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/services/indexer

# Произвольный UID (OpenShift): файлы принадлежат группе 0 с правом записи, как и у основного образа
RUN useradd --create-home --uid 10001 --gid 0 app

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src
COPY services/indexer/alembic.ini ./services/indexer/alembic.ini
COPY services/indexer/beauty_indexer ./services/indexer/beauty_indexer
RUN rm -rf ./services/indexer/beauty_indexer/tests && chmod -R g=u /app

USER 10001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    --start-period=10s --start-interval=1s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8001/health')"]

# Воркер (Kafka, обход сайтов, relay): docker run … python -m beauty_indexer.worker
# Миграции схемы knowledge: docker run … alembic -c services/indexer/alembic.ini upgrade head
CMD ["uvicorn", "beauty_indexer.api:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8001", \
     "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "25"]
