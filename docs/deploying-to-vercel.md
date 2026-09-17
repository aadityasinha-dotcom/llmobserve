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

`apps/api/vercel.json` does the rest: every path is rewritten to
`api/index.py`, and functions run in **`bom1` (Mumbai)**, next to the Supabase
project. Leave the region there. From the default `iad1` every query crosses
the planet, and ingest issues several per request.

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

Do not add `SUPABASE_MIGRATION_URL` or any `postgres`-role URL. A function that
serves traffic has no use for owner credentials. The `postgres` role also has
`BYPASSRLS`, so connecting as it switches off tenant isolation with no error.
The API now checks for that on the first tenant request of every instance and
answers **503** until it is fixed. That check replaces the `/readyz` gate,
which Vercel does not have.

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
| `LLMOBSERVE_API_KEY` | a project key from `make key` |

Neither is `NEXT_PUBLIC_`, so neither reaches the browser. The dashboard calls
the API from the server.

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
| SDK: silent, nothing lands | `LLM_METRICS_DEBUG` unset. Set it and re-run |
| `ModuleNotFoundError` in function logs | A runtime dependency is missing from `apps/api/requirements.txt`. `tests/test_vercel_packaging.py` should have caught it |
| Password looks right but auth fails | An unencoded `@`, `:` or `/` in the password. Check with `sqlalchemy.engine.url.make_url()` |
