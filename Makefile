# Единые команды для разработчика и CI. Список команд: make help

RUN := poetry run
# --env-file: подставлять в docker-compose.yml значения из .env в корне проекта
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test check up down ps logs migrate

help:
	@echo make install   - install dependencies and git hooks
	@echo make lint      - ruff: errors, style and formatting
	@echo make format    - ruff: fix and format the code
	@echo make typecheck - mypy in strict mode
	@echo make test      - pytest with coverage, minimum 80 percent
	@echo make check     - lint, typecheck and test
	@echo make up        - build and start services in Docker, wait until healthy
	@echo make down      - stop services, data in volumes is kept
	@echo make ps        - service status
	@echo make logs      - follow api logs
	@echo make migrate   - apply database migrations

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
	$(RUN) pytest --cov

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
