# Единые команды для разработчика и CI. Список команд: make help

RUN := poetry run
# --env-file: подставлять в docker-compose.yml значения из .env в корне проекта
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test audit migrate-check check up down ps logs migrate chat evals seed worker

help:
	@echo make install   - install dependencies and git hooks
	@echo make lint      - ruff: errors, style and formatting
	@echo make format    - ruff: fix and format the code
	@echo make typecheck - mypy in strict mode
	@echo make test      - pytest with coverage, minimum 80 percent
	@echo make audit     - pip-audit: known vulnerabilities in dependencies
	@echo make migrate-check - alembic check: models changed but no migration
	@echo make check     - lint, typecheck and test
	@echo make up        - build and start services in Docker, wait until healthy
	@echo make down      - stop services, data in volumes is kept
	@echo make ps        - service status
	@echo make logs      - follow api logs
	@echo make migrate   - apply database migrations
	@echo make chat      - chat with the agent in the terminal on the fake CRM
	@echo make evals     - run evals, MODELS=offline,qwen3-235b DATASET=booking/v1
	@echo make seed      - demo salon from the fake CRM: /s/demo-lavanda
	@echo make worker    - Celery worker locally, without Docker

install:
	poetry install
	$(RUN) pre-commit install

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

typecheck:
	$(RUN) mypy

test:
	$(RUN) pytest --cov --cov-report=term-missing --cov-report=xml

# --skip-editable: сам проект не опубликован в PyPI, проверять в нём нечего
audit:
	$(RUN) pip-audit --skip-editable --progress-spinner off

# Нужна база со схемой: локально — make up, в CI — сервис postgres
migrate-check:
	$(RUN) alembic upgrade head
	$(RUN) alembic check

check: lint typecheck test

up:
	$(COMPOSE) up --build --wait

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f api

migrate:
	$(RUN) alembic upgrade head

chat:
	$(RUN) python -m scripts.chat_cli --debug

MODELS ?= offline
DATASET ?= booking/v1

evals:
	$(RUN) python -m evals.run --dataset $(DATASET) --models $(MODELS)

seed:
	$(RUN) python -m scripts.seed_demo

# --pool=solo: на Windows prefork Celery не работает
worker:
	$(RUN) celery -A beauty_agent.workers.celery_app worker -Q messages,periodic --pool=solo --loglevel=INFO
