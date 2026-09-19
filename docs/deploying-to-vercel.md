# Deploying to Vercel

Two Vercel projects from this one repo: the API (`apps/api`) and the dashboard
(`apps/web`). Postgres stays on Supabase. Both fit Vercel's Hobby tier.

```
SDK ──HTTPS──▶  llm-observe-api (Vercel, bom1)  ──6543──▶  Supabase (ap-south-1)
                        ▲
browser ──▶  llm-observe-web (Vercel) ── server-side fetch, key never in browser
```

---

## 0. Before you start

- **Commit and push.** A Git-based deploy only sees what is on GitHub.
- **Migrations are already applied** to Supabase. Check with
  `make local-migrate`, which is a no-op at head. Vercel never runs migrations.
  Run them from your machine before any deploy that adds one.

---

## 1. The API project

In the Vercel dashboard: **Add New → Project → import the repo**, then:

| Setting | Value |
|---|---|
| Project name | `llm-observe-api` |
| Root Directory | `apps/api` |
| Framework Preset | Other |
| Build / Output / Install | leave empty |

Nothing else. Vercel detects the FastAPI app at `app/main.py` — `main.py`
inside `app/` is one of its auto-detected entrypoints — and routes **every**
path to it, so `/healthz`, `/v1/ingest` and the rest work as they do under
uvicorn.

`apps/api/vercel.json` only tunes that function: `maxDuration`, `excludeFiles`,
and the **`bom1` (Mumbai)** region, next to the Supabase project. Leave the
region there. From the default `iad1` every query crosses the planet, and ingest
issues several per request.

**Do not add a `rewrites` block.** Routing all paths to a function
(`{"source": "/(.*)", "destination": "/api/index"}`) replaces the path the ASGI
app receives, so FastAPI sees `/api/index` for every request and returns 404 for
all of them — including `/healthz` and `/openapi.json`, which looks like the app
failed to load rather than a routing mistake. That is how the first deployment
of this project failed. `tests/test_vercel_packaging.py` now fails if a
`rewrites` or `routes` block reappears.

### Environment variables (Production)

| Name | Value |
|---|---|
| `DATABASE_URL` | the **same value as in your local `.env`** |
| `DB_POOL_MODE` | `transaction` |
| `DB_REQUIRE_RLS` | `true` |
| `ENVIRONMENT` | `production` |

`DATABASE_URL` must be the **transaction pooler** (port `6543`) and the
**`llmobserve_app.<project-ref>`** user:

```
postgresql+asyncpg://llmobserve_app.zmsijiqvrhvqfofomigb:<password>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?ssl=require
```

Values are validated on load. Surrounding quotes and stray whitespace are
stripped before validation, so a pasted `"true"` is fine — but the value itself
must be one the field accepts:

| Variable | Accepted values |
|---|---|
| `DB_POOL_MODE` | `session` or `transaction` — nothing else, and case-sensitive |
| `DB_REQUIRE_RLS` | `true` / `false` (also `1` / `0`, `yes` / `no`); case-insensitive |
| `ENVIRONMENT` | any string |
| `INGEST_MAX_*` | integers |

A value outside that set makes every settings-dependent route answer **503**
with the offending variable named:

```json
{"detail":{"status":"config_error","invalid_env_vars":["DB_POOL_MODE"], ...}}
```

`/openapi.json` keeps working, because it does not read settings — so
"`/openapi.json` 200 but `/healthz` failing" means a bad environment variable,
not a broken deployment.

Do not add `SUPABASE_MIGRATION_URL` or any `postgres`-role URL. A function that
serves traffic has no use for owner credentials. The `postgres` role also has
`BYPASSRLS`, so connecting as it switches off tenant isolation with no error.
The API checks for that on the first tenant request of every instance and
answers **503** until it is fixed. Vercel supports FastAPI lifespan events, but
it has no readiness probe — nothing consults `/readyz` before sending traffic —
so the in-request check is what actually keeps a misconfigured deployment from
serving cross-tenant data.

Deploy, then:

```bash
API=https://llm-observe-api.vercel.app      # your production domain
curl -s $API/healthz        # {"status":"ok","environment":"production"}
curl -s $API/readyz         # {"status":"ready"}  <- proves RLS is enforced
```

If `/readyz` returns 500, read the function logs before doing anything else.
It means the role can bypass RLS or the database is unreachable.

---

## 2. The dashboard project

Import the **same repo** a second time:

| Setting | Value |
|---|---|
| Project name | `llm-observe-web` |
| Root Directory | `apps/web` |
| Framework Preset | Next.js (auto-detected) |
| Node.js version | default (22.x) is fine |

### Environment variables (Production)

| Name | Value |
|---|---|
| `API_BASE_URL` | `https://llm-observe-api.vercel.app`, no trailing slash |
| `LLMOBSERVE_API_KEY` | a **read-only** key: `make key NAME=<project> ARGS="--add --scopes read --label dashboard"` |

Neither is `NEXT_PUBLIC_`, so neither reaches the browser. The dashboard calls
the API from the server.

### Lock the dashboard down before sharing the URL

The dashboard has no authentication of its own. It holds one project API key
server-side and renders everything that key can read. Give it a **read-only**
key so a leak cannot be used to forge traces, and so revoking it does not touch
the SDK's - including the full
`input` and `output` payloads on the trace detail page, which are your prompts
and model responses. A public URL is therefore a public read of every trace in
that project.

On the **web** project: **Settings → Deployment Protection → Vercel
Authentication**, scope **All Deployments**. That covers the production domain,
not just previews, and is free on every plan. Access is then limited to users
signed in to the Vercel account.

**Do not enable this on the API project.** The SDK, CI and anything else calling
`/v1/ingest` are not signed in to Vercel, so protecting the API's production
domain returns Vercel's login page to them instead of the API - the SDK reads
that 401 as a bad key and silently drops the batch. Leave the API on the default
scope, which leaves production domains reachable.

When the dashboard eventually grows real per-user login, this becomes redundant;
until then it is the only thing standing between the URL and the payloads.

---

## 3. Point the SDK at it

```bash
export LLM_METRICS_HOST=https://llm-observe-api.vercel.app
export LLM_METRICS_API_KEY=llmo_sk_...
export LLM_METRICS_DEBUG=1
~/git-repo/llmobserve-python/.venv/bin/python examples/hello_trace.py
```

Then open the dashboard's production URL. The trace should be at the top.

---

## Things that will bite

**Use the production domain, not a deployment URL.** Vercel's Deployment
Protection puts its own login page in front of per-deployment URLs
(`llm-observe-api-abc123-you.vercel.app`). A request there gets Vercel's 401,
not the API's. The SDK treats that as a bad key and drops the batch, and the
dashboard shows "API key rejected". The project's production domain
(`llm-observe-api.vercel.app`) is not protected under the default setting. If
calls to it still return Vercel's login page, check **Settings → Deployment
Protection** on the API project.

**Request bodies are capped at 4.5 MB** by Vercel, before any of our code runs.
The SDK's defaults stay well under that: `flush_at=100` events per batch, with
input and output truncated to 2,000 characters each. Raising both
`flush_at` and `max_value_chars` can cross the limit. When that happens the SDK
gets a 413, classifies it as permanent, and **drops the batch**. You only see
it with `LLM_METRICS_DEBUG=1`.

**Cold starts.** The first request to an idle instance pays for Python start-up,
a new pooler connection and the RLS check. Expect a slow first call and fast
calls after it.

**The eval worker cannot run here.** `apps/worker` will be a long-lived arq
consumer, and Vercel has no long-lived processes. When evals start, the worker
needs a host like Cloud Run, Render or Fly. The API can stay on Vercel and
enqueue to a hosted Redis.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `/readyz` 500, tenant routes 503 | Runtime role can bypass RLS, or the DB is unreachable. Check `DATABASE_URL` user is `llmobserve_app.<ref>` |
| `DuplicatePreparedStatementError` | `DB_POOL_MODE` is not `transaction` while using port 6543 |
| Every call slow, not only the first | Function region is not `bom1`. Check the deployment's function region |
| Dashboard: "Backend unreachable" | `API_BASE_URL` wrong, or has a trailing path |
| Dashboard: "API key rejected" | Key not issued against this database, or you hit a protected deployment URL |
| Dashboard: "API key cannot read traces" | The key lacks the `read` scope. Issue one with `ARGS="--add --scopes read"` |
| SDK logs a 403 | The key lacks the `ingest` scope |
| SDK: silent, nothing lands | `LLM_METRICS_DEBUG` unset. Set it and re-run |
| `ModuleNotFoundError` in function logs | A runtime dependency is missing from `apps/api/requirements.txt`. `tests/test_vercel_packaging.py` should have caught it |
| **Every path 404s with `{"detail":"Not Found"}`** | A `rewrites`/`routes` block in `vercel.json` is rewriting the path. Remove it; Vercel routes to the entrypoint on its own |
| Every path 404s with Vercel's own HTML 404 | No entrypoint detected. The app must be at `app/main.py` (or another detected name) and export `app` |
| `/openapi.json` 200 but `/healthz` 503 `config_error` | Bad environment variable. The response names it |
| `/openapi.json` 200 but `/healthz` 500 `Internal Server Error` | Same cause, on a build older than the `config_error` response. Check the function logs for `ValidationError` |
| Password looks right but auth fails | An unencoded `@`, `:` or `/` in the password. Check with `sqlalchemy.engine.url.make_url()` |
