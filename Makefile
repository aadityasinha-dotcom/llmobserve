.PHONY: dev dev-supabase down logs migrate migrate-remote revision test lint fmt psql shell check-rls \
        venv serve key local-migrate local-test

COMPOSE := docker compose
API := $(COMPOSE) exec -T api
# --no-deps so remote-target commands do not spin up the local database.
RUN_API := $(COMPOSE) run --rm --no-deps -T api

# ---------------------------------------------------------------------------
# Local (no Docker) targets.
#
# The API needs Python 3.11+; the system interpreter here is 3.10. `uv` fetches
# a standalone one rather than requiring a system upgrade, so the whole stack -
# server, migrations, tests - runs against Supabase with no daemon involved.
# The container targets above remain the way to test the image itself.
# ---------------------------------------------------------------------------
VENV := apps/api/.venv
PY := $(VENV)/bin/python
LOCAL := set -a; . ./.env; set +a; cd apps/api;

$(VENV):
	@command -v uv >/dev/null 2>&1 || python3 -m pip install --quiet uv
	uv python install 3.12
	uv venv --python 3.12 $(VENV)
	@# `uv venv` deliberately omits pip, so install through uv rather than into it.
	uv pip install --python $(VENV)/bin/python -e "apps/api[dev]"
	@echo "venv ready: $(VENV)  (python $$($(PY) -V | cut -d' ' -f2))"

venv: $(VENV)

# Serve the API in the foreground. Ctrl-C stops it.
serve: $(VENV)
	@test -f .env || { echo "No .env - copy .env.example and fill it in."; exit 1; }
	$(LOCAL) ../../$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Issue an API key:  make key NAME=my-project
#   ARGS=--rotate    new key for an existing project, KEEPING its traces
#   ARGS=--replace   recreate the project, DELETING its traces
key: $(VENV)
	@test -n "$(NAME)" || { echo 'usage: make key NAME=my-project [ARGS=--rotate]'; exit 1; }
	@$(LOCAL) ../../$(PY) scripts/create_project.py "$(NAME)" $(ARGS)

local-migrate: $(VENV)
	$(LOCAL) MIGRATION_DATABASE_URL="$${MIGRATION_DATABASE_URL:-$$SUPABASE_MIGRATION_URL}" \
		../../$(VENV)/bin/alembic upgrade head

local-test: $(VENV)
	$(LOCAL) ../../$(PY) -m pytest $(ARGS)

dev:
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d --build
	@echo "api on http://127.0.0.1:8000  (docs: /docs)"

# Run the API against Supabase instead of the local container.
#
# --no-deps skips the local postgres service; DATABASE_URL from .env is what the
# app connects to. Redis is still local - Supabase does not provide it.
dev-supabase:
	@grep -qE '^DATABASE_URL=.*supabase' .env 2>/dev/null || { \
		echo "DATABASE_URL in .env does not point at Supabase."; \
		echo "Uncomment the SUPABASE block in .env.example first."; exit 1; }
	$(COMPOSE) up -d --build --no-deps api redis
	@echo "api on http://127.0.0.1:8000 -> supabase"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

# Migrate the local Docker database.
migrate:
	$(API) alembic upgrade head

# Migrate a remote target (Supabase) using SUPABASE_MIGRATION_URL from .env.
#
# Runs inside the api container because the host has Python 3.10 and no Alembic,
# while apps/api needs 3.11+. --no-deps keeps the local Postgres out of it.
# The variable is expanded inside the container, where env_file has already
# placed it, so the credential never lands in the host shell's history.
migrate-remote:
	$(RUN_API) sh -c '\
		test -n "$$SUPABASE_MIGRATION_URL" || { \
			echo "SUPABASE_MIGRATION_URL is not set in .env"; exit 1; }; \
		MIGRATION_DATABASE_URL="$$SUPABASE_MIGRATION_URL" alembic upgrade head'

# Confirm the runtime role cannot bypass RLS. Reads DATABASE_URL, so it checks
# whichever target .env currently points at - local by default, Supabase once
# DATABASE_URL is set there.
check-rls:
	$(RUN_API) python -c "\
import asyncio; \
from app.db import check_database_ready; \
asyncio.run(check_database_ready()); \
print('RLS enforced for the runtime role')"

revision:
	@test -n "$(m)" || (echo 'usage: make revision m="message"'; exit 1)
	$(API) alembic revision --autogenerate -m "$(m)"

test:
	$(API) pytest

lint:
	$(API) ruff check app
	$(API) mypy app

fmt:
	$(API) ruff format app
	$(API) ruff check --fix app

psql:
	$(COMPOSE) exec postgres psql -U postgres -d llmobserve

shell:
	$(COMPOSE) exec api bash
