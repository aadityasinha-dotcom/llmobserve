# Testing the API with the Python SDK

End-to-end loop: run `apps/api`, point the `llmobserve` SDK at it, confirm a
trace lands in Postgres.

The SDK **fails silently by design** — if delivery fails, your program still
runs and returns normally. So the first rule of testing this loop is:

```bash
export LLMOBSERVE_DEBUG=1
```

Without it, a rejected batch looks exactly like a working one, and the only
symptom is an empty database.

---

## Two ways to run

| | Docker | Local (`make venv`) |
|---|---|---|
| Needs the Docker daemon | yes | no |
| Python | in the image | 3.12, fetched by `uv` |
| Database | local Postgres container, or Supabase | Supabase |
| Use when | testing the image, or you want local Postgres | day-to-day development |

The local path exists because the API needs Python 3.11+ and this machine's
system interpreter is 3.10. `uv` fetches a standalone one rather than requiring
a system upgrade. Everything below shows the local path first, with the Docker
equivalent alongside.

---

## 1. Set up `.env`

```bash
cp .env.example .env
```

Fill in the Supabase block (or leave the local Docker defaults). Two variables
matter here:

- `DATABASE_URL` — the **restricted** role the API connects as. Must not be an
  admin role; see step 4.
- `SUPABASE_MIGRATION_URL` — the **owner** connection, used only by Alembic and
  by `create_project.py`.

Passwords are URL-encoded. An unencoded `@` does not error — it truncates the
password at the first `@` and folds the rest into the hostname, which surfaces
later as a confusing DNS or auth failure. Generate alphanumeric passwords and
the problem cannot arise:

```bash
openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32
```

---

## 2. Build the local toolchain

```bash
make venv          # uv installs Python 3.12 into apps/api/.venv
```

One-off, ~1 minute. Gitignored. Skip this entirely if you are using Docker.

---

## 3. Apply migrations

```bash
make local-migrate           # local venv  -> Supabase
# or
make migrate                 # Docker      -> local Postgres container
make migrate-remote          # Docker      -> Supabase
```

New Supabase project? Two things must happen **before** the first migration:

1. Run `infra/supabase/01_bootstrap_app_role.sql` in the SQL Editor, with a
   generated password substituted for both placeholders. Migration `0001`
   grants privileges to `llmobserve_app` and fails if the role does not exist.
2. Enable `pg_cron` under Database → Extensions, so migration `0002` can
   schedule daily partition maintenance. Without it that migration is a
   deliberate no-op and you must call
   `ensure_observations_partitions(30, 8)` from somewhere else.

---

## 4. Confirm tenant isolation is real

This is the step people skip, and it is the one that matters.

```bash
curl -s http://127.0.0.1:8000/readyz     # once the API is up (step 6)
# {"status":"ready"}
```

`/readyz` fails if the runtime role can bypass row-level security. That failure
is otherwise **completely silent**: every policy can be correctly defined and
enforce nothing, queries succeed, data comes back, and nothing looks wrong. The
realistic way to get there is pasting the connection string Supabase offers by
default — its `postgres` role has `rolbypassrls = true`.

Under Docker there is also `make check-rls`. Or check directly:

```sql
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
WHERE rolname IN ('postgres', 'llmobserve_app');
-- llmobserve_app MUST be f | f
```

---

## 5. Issue an API key

```bash
make key NAME=my-app
```

```
project    my-app
project_id 92764a44-f76f-43d2-ac3c-6acec1891c20
api_key    llmo_sk_HAYmflQiRmJZYYHWm1jaXSIk2_sCiP03muLpcTZMVHg
```

**Shown once.** Only a SHA-256 hash is stored, so a dump of `projects` cannot be
replayed against the API — and a lost key cannot be recovered, only reissued
with `--replace` (which deletes that project's traces).

This needs the owner connection because `projects` is deliberately outside RLS
— authentication has to resolve a key before any tenant is known — and the
application role holds `SELECT` on it and nothing else.

---

## 6. Start the API

```bash
make serve                   # local venv, foreground, --reload
# or
make dev                     # Docker + local Postgres
make dev-supabase            # Docker, API only, against Supabase
```

```bash
curl -s http://127.0.0.1:8000/healthz    # {"status":"ok",...}   never touches the DB
curl -s http://127.0.0.1:8000/readyz     # {"status":"ready"}    DB + RLS check
open http://127.0.0.1:8000/docs          # interactive OpenAPI
```

---

## 7. Point the SDK at it

```bash
export LLMOBSERVE_API_KEY=llmo_sk_...    # from step 5
export LLMOBSERVE_HOST=http://127.0.0.1:8000
export LLMOBSERVE_DEBUG=1
```

```python
import time

import llmobserve
from llmobserve import observe

@observe(as_type="generation", name="chat")
def chat(prompt: str) -> str:
    time.sleep(0.05)          # stand-in for a real call, so latency is visible
    return f"echo: {prompt}"

@observe(name="request")
def request(prompt: str) -> str:
    return chat(prompt)

request("hello")
llmobserve.flush()      # the buffer is async; flush before the process exits
llmobserve.shutdown()
```

```bash
cd ~/git-repo/llmobserve-python && .venv/bin/python your_script.py
```

`flush()` matters. The SDK buffers and delivers on a background thread, so a
short script can exit before anything is sent. For faster feedback while
testing, `llmobserve.configure(flush_at=1, flush_interval=0.5)` sends each
event immediately instead of waiting for a full batch.

Clean output means delivery succeeded. Anything else looks like:

```
llmobserve: dropped 1 event(s): HTTP 422
```

---

## 8. Confirm it landed

Server side, you should see `202 Accepted` per batch. Then:

```sql
SELECT p.name AS project, t.name AS trace, o.name AS observation,
       o.type, o.level, o.latency_ms, o.total_tokens, o.cost_usd,
       o.tableoid::regclass AS partition
FROM projects p
JOIN traces t ON t.project_id = p.id
LEFT JOIN observations o ON o.trace_id = t.id
WHERE p.name = 'my-app'
ORDER BY o.started_at;
```

```
 project |  trace  | observation |    type    | level | latency_ms | total_tokens | cost_usd |       partition
---------+---------+-------------+------------+-------+------------+--------------+----------+------------------------
 my-app  | request | request     | span       | ok    |         50 |            0 |          | observations_p20260818
 my-app  | request | chat        | generation | ok    |         50 |            0 |          | observations_p20260818
```

Three things worth checking in that output:

- **`partition` is `observations_pYYYYMMDD`, not `observations_default`.** A row
  in the default partition permanently costs that day its partition — the
  maintenance function skips any day whose rows are already stranded, so every
  later write for that day also lands in DEFAULT and stops being pruned.
- **`level` is `ok`.** That is the SDK's `status` field; `error` means the
  decorated function raised.
- **`cost_usd` is null above, and that is correct here** — the toy `chat()`
  reports no `model` and no token counts, so there is no basis for a cost. Null
  means unknown, never free. Instrument a real provider call (or set `model`,
  `prompt_tokens` and `completion_tokens` yourself) and it fills in.

---

## Troubleshooting

Every failure mode here is quiet. That is why the table exists.

| Symptom | Cause | Fix |
|---|---|---|
| No output at all, empty database | `LLMOBSERVE_DEBUG` unset | Set it. The SDK never raises on delivery failure. |
| `dropped N event(s): HTTP 401` | Key not recognised | Reissue with `make key`. 401 is deliberately identical for a missing header, wrong scheme, and unknown key. |
| `HTTP 422` mentioning `traces`/`events` | Envelope not recognised | The body must carry `events` or `traces`. This 422 is intentional — see below. |
| `HTTP 422` on a specific field | Type mismatch | Read `loc` in the response body; it names the exact field. |
| `HTTP 413` | Batch too large | Lower `flush_at`, or raise `INGEST_MAX_OBSERVATIONS_PER_BATCH`. |
| 202 but nothing in the database | Wrong project, or observations rejected | Check `rejected_observations` in the response body. |
| `rejected_observations > 0` | Timestamp outside the accepted window | Client clock skew. Retrying cannot help — the event only ages further out of range. |
| Rows visible across projects | Runtime role bypasses RLS | `DATABASE_URL` points at an admin role. `/readyz` catches this. |
| Script exits, nothing sent | No `flush()` | Call `llmobserve.flush()` before exit. |

### Why an unrecognised envelope is a 422

Everything else here is lenient — unknown *fields* are ignored so that a newer
SDK can send keys this server has never heard of. But a body with none of
`events` / `traces` / `observations` is rejected outright, because the
alternative is 202 with a count of zero on every batch: success reported while
nothing is stored. An empty `{"events": []}` is fine; the key itself is not
optional.

---

## The wire contract

`/v1/ingest` accepts two envelopes. The SDK sends the first.

```jsonc
// Flat — one stream, discriminated by `type`
{"events": [
  {"id": "…", "type": "trace", "name": "request", "start_time": "…"},
  {"id": "…", "type": "generation", "trace_id": "…", "parent_id": "…", "status": "ok"}
]}

// Nested — observations carried inside their trace
{"traces": [
  {"id": "…", "name": "request", "observations": [{"id": "…", "type": "generation"}]}
]}
```

The flat form suits a buffering client: spans finish independently and flush in
whatever order they complete. Requiring the nesting would force the SDK to hold
a trace open until its last child returned, so a long-running trace could not be
reported until it ended.

Field mappings that are easy to miss:

| SDK sends | Stored as |
|---|---|
| `status` (`"ok"` / `"error"`) | `level` |
| `parent_id` | `parent_observation_id` |
| `start_time` / `end_time` | `started_at` / `ended_at` |
| `input_tokens` / `gen_ai.usage.input_tokens` | `prompt_tokens` |
| `latency_ms: 50.4779…` (float) | rounded to `50` |

Numeric fields accept any JSON number and are stored as rounded non-negative
integers. `openapi.json` publishes them as `number` rather than `integer` for
exactly this reason — the SDK times spans with `perf_counter` and legitimately
sends fractional milliseconds.

`cost_usd` is **never** read from the payload. It is computed server-side at
write time from the token counts and the pricebook in `app/services/pricing.py`,
then frozen. A client that sends `cost_usd` has it silently dropped.

### Idempotency requires a client timestamp

Retries are safe **when the SDK sends `start_time`**. The observations primary
key is `(project_id, id, started_at)` — Postgres requires the partition key in
every unique constraint — so an observation sent without a timestamp gets a
server-stamped one, and a retry becomes a genuinely different row. The SDK sets
`start_time` on every event, so this only bites hand-rolled clients.

---

## Automated tests

```bash
make local-test                                   # all 77
make local-test ARGS="tests/test_ingest.py -q"    # one file
make test                                         # Docker equivalent
```

| File | Covers |
|---|---|
| `test_pricing.py` | Pricebook resolution and cost arithmetic. No database. |
| `test_ingest.py` | Round trip, idempotency, out-of-order delivery, partition window, envelopes. |
| `test_tenant_isolation.py` | Project A cannot read project B's traces. |

These run against a **real** Postgres, deliberately — the isolation property is
enforced by row-level security, so a mock or SQLite would assert nothing. Each
test creates its own projects and deletes them afterwards.

`make local-test` takes ~100s against Supabase, essentially all network round
trips. Against a local Postgres container it is much faster.
