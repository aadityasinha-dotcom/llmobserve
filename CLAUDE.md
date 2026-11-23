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

The end-to-end loop is built and deployed: the SDK ingests into the API on
Vercel, Postgres on Supabase stores it under row-level security, and the
dashboard on Vercel lists traces, shows a trace's span tree, token detail and
scores, and issues scoped API keys. Sign-in is Google OAuth with per-user
project membership. See `docs/deploying-to-vercel.md` and
`docs/google-sign-in.md` for the live setup.

Next is the eval path: `apps/worker` (arq) consuming a queue the API enqueues
to, scorers writing `scores`, and metrics aggregation for the dashboard. None
of that is started. Do not scaffold `apps/worker` until the queue contract is
designed; the dashboard's own rules are in `apps/web/CLAUDE.md`.

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

## Milestones reached

1. `docker-compose.yml` — postgres + redis for local runs; Supabase in production
2. Alembic migrations `0001`–`0009`: projects, traces (day-partitioned
   observations), api_keys, users, project_members, scores
3. `app/deps.py` — bearer key or session JWT resolves to a project, with RLS
   enforced per request and a per-process check that the role cannot bypass it
4. `app/routers/ingest.py` — POST /v1/ingest, batched, idempotent, cost frozen
5. `app/routers/traces.py` — GET /v1/traces (keyset cursor) and /v1/traces/{id}
6. `app/routers/auth.py`, `keys.py` — Google sign-in, sessions, key management
7. `tests/test_tenant_isolation.py` — project A cannot read project B's traces
8. `apps/web` — trace list and detail, settings, sign-in; deployed on Vercel
9. The `llm-metrics` SDK sends a real OpenAI / Anthropic call through all of it

Migrations are applied from a developer machine (`make local-migrate`), never
by a deploy. Run a new one before pushing the code that depends on it.

## Out of scope for now

Eval worker and scorers, metrics aggregation and charts, prompt versioning
(the `prompts` table), Cloud Run deploy config for the worker. They are in the
architecture but not built, and nothing should pretend they are.
