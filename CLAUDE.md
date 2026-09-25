# llm-observe

Self-hostable LLM observability platform: ingests traces from the `llm-metrics`
Python SDK (imported as `llm_metrics`), stores them in Postgres, scores them
with background eval workers, and surfaces cost / latency / quality in a
Next.js dashboard.

Monorepo with three deploy targets:

- `apps/api`    — FastAPI. Ingest + query. Deploys to Vercel (Root Directory =
                 apps/api); the Dockerfile remains for local and container runs.
- `apps/worker` — arq worker. Runs eval scorers. Deploys to Cloud Run.
- `apps/web`    — Next.js dashboard. Deploys to Vercel (Root Directory = apps/web).

The SDK lives in a separate repo (`llmobserve-python`, a directory name that
predates the package rename) and is versioned independently. Do not add SDK
code here.

## Current phase

Building the minimum ingest path so the SDK can be tested end to end. In scope
right now: auth, POST /v1/ingest, persistence. Evals, dashboard, and metrics
come after. Do not scaffold apps/worker or apps/web yet.

## Non-negotiable design rules

1. **Ingest is the fast path.** POST /v1/ingest writes and returns. No LLM calls,
   no scoring, no expensive aggregation inline. Eval work is enqueued, never run
   in the request.
2. **Every query is project-scoped.** API key resolves to a project_id. Every
   table carries project_id and every read filters on it. Enforce with Postgres
   row-level security, not by trusting WHERE clauses.
3. **Cost is computed server-side and frozen.** Read token counts from the
   payload, look up the price at write time, store the resulting cost_usd.
   Never recompute historical cost from current prices.
4. **Ingest is idempotent.** Clients retry. Duplicate event ids must not create
   duplicate rows. Use an idempotency key with ON CONFLICT DO NOTHING.
5. **Accept unknown fields.** Older SDK versions send older payloads and newer
   ones may send extra keys. Never 4xx on an unrecognised field. The same goes
   for over-long or oddly-typed attribution values: trim or drop the value,
   never the batch.

## Data model

- `projects`     — id, name (the tenant root; `project_id` is the identity)
- `api_keys`     — id, project_id, key_hash, key_prefix, label, scopes[],
                   revoked_at. Many per project. Scopes are `ingest` (write) and
                   `read` (query), so a key shipped inside a client cannot read
                   stored payloads back. Store a hash, never the raw key
- `users`        — id, google_sub (the identity; email can change), email, name,
                   session_version (bumped on sign-out to revoke every session)
- `project_members` — project_id, user_id, role (owner | member). A signed-in
                   user sees only projects they are a member of. Users, projects,
                   memberships and keys are written only through SECURITY DEFINER
                   functions (migration 0007); the app role has no INSERT on them
- `traces`       — id, project_id, name, user_id, session_id, tags[],
                   environment, release, metadata, timestamps
- `observations` — id, trace_id, parent_id (nestable), model, input, output,
                   prompt_tokens, completion_tokens, cached_tokens,
                   reasoning_tokens, latency_ms, cost_usd, prompt_name,
                   prompt_version. cached/reasoning are *subsets* of the two
                   totals, never additions to them
- `scores`       — id, trace_id or observation_id (no FK to either), name,
                   data_type (numeric | boolean | categorical), value (numeric,
                   booleans as 1/0), value_text, comment, source (human |
                   llm_judge | heuristic, unconstrained), scored_at. Idempotent
                   on (project_id, id); INSERT only, no update path
- `prompts`      — versioned templates for A/B comparison (not built yet;
                   observations.prompt_name/prompt_version are the key it will
                   join on)

Field naming follows OpenTelemetry GenAI semantic conventions where applicable.
Payload bodies go in JSONB. Partition `observations` by day.

## Contract with the SDK

The API and SDK version independently. `apps/api/openapi.json` is generated
from the schemas (`make openapi`) and committed; the SDK repo validates its
request payloads against it. Regenerate it after any change under
`app/schemas/`. Breaking the ingest schema breaks the SDK's CI — treat that as
the signal it is.

- Endpoint is versioned: `/v1/ingest`
- Clients send `X-SDK-Version`; log it so deprecation decisions have data
- Auth is `Authorization: Bearer <api_key>`; the key must carry the `ingest`
  scope. 401 means unknown or revoked, 403 means wrong scope

## Conventions

- Python 3.11+ for the backend (the SDK supports 3.9, the server does not need to)
- SQLAlchemy 2.0 style, async engine, Alembic for migrations
- Pydantic v2 for request/response schemas, kept separate from ORM models
- ruff + mypy, both must pass
- Secrets from env only, never committed. See .env.example

## Commands

See `docs/testing-with-the-sdk.md` for the full end-to-end loop, including
running without Docker.
See `docs/deploying-to-vercel.md` for deploying the API and dashboard.
See `docs/google-sign-in.md` for dashboard sign-in and per-user access.

```bash
make dev        # docker compose up: postgres + redis + api
make migrate    # alembic upgrade head
make revision   # alembic revision --autogenerate
make test       # pytest
make lint       # ruff + mypy
```

Without Docker (the API needs 3.11+; `uv` fetches it, nothing is installed
system-wide):

```bash
make venv              # one-off: apps/api/.venv
make serve             # uvicorn on 127.0.0.1:8000, --reload
make key NAME=my-app   # issue a project + API key
make local-migrate     # alembic upgrade head
make local-test        # pytest   (ARGS="tests/test_ingest.py -q")
```

## Build order (current phase)

1. `docker-compose.yml` — postgres + redis, nothing else yet
2. Alembic init + first migration: projects, traces, observations
3. `app/deps.py` — API key auth, resolves bearer token to project_id
4. `app/schemas/ingest.py` — Pydantic models for the batch payload
5. `app/services/pricing.py` — model → cost table, frozen at write
6. `app/routers/ingest.py` — POST /v1/ingest, batched, idempotent
7. `tests/test_tenant_isolation.py` — project A cannot read project B's traces
8. Point the SDK at localhost:8000 and confirm a trace lands in Postgres

Step 8 is the milestone. Everything before it is in service of that.

## Out of scope for now

Dashboard, eval worker, scorers, metrics aggregation, prompt versioning,
Cloud Run deploy config. They are in the architecture but not this phase.
