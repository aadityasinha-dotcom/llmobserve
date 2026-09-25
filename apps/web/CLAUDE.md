# apps/web

Next.js dashboard for the llm-observe platform. Reads traces, observations, and
scores from the FastAPI backend and surfaces cost, latency, and quality.

This directory contains ONLY the dashboard. The FastAPI backend is a sibling at
`apps/api` and the eval worker will be at `apps/worker`; the Python SDK lives in
the separate `llmobserve-python` repo. Never add backend or SDK code here - the
deploy targets are separate even though the repo is shared.

## Current phase

The two views needed to see SDK traces end to end exist: a filterable trace
list and a trace detail page with a nested span tree, scores, and token
detail. Metrics charts, eval views, and prompt A/B comparison come later. Do
not scaffold them yet.

## Non-negotiable design rules

1. **Never hand-write API types.** All request and response types are generated
   from the backend's `openapi.json` into `lib/api/schema.d.ts`. If a type is
   missing, regenerate — do not add an interface by hand. This is the only thing
   keeping the two repos in sync.
2. **No secrets in the client bundle.** The API key never reaches the browser.
   Calls to the backend go through Next.js route handlers or server components
   that read the key from a server-only env var.
3. **Server components by default.** Add `"use client"` only where interactivity
   actually requires it — filters, the span tree's expand/collapse, charts.
4. **Every list is paginated.** Trace tables will hit tens of thousands of rows.
   Cursor pagination, never offset, and never fetch an unbounded list.
5. **Loading and empty states are required, not polish.** A fresh install has
   zero traces. The empty state should tell the user how to send their first one.

## Stack

- Next.js 15, App Router, TypeScript strict mode
- Tailwind CSS + shadcn/ui
- TanStack Query for client-side fetching and cache
- Recharts for charts (later phase)
- `openapi-typescript` for type generation
- Vitest + Testing Library
- Deploys to Vercel with the project's Root Directory set to `apps/web`

## Contract with the backend

The backend publishes `openapi.json` as a CI artifact. Now that both live in one
repo you can also generate it straight from `apps/api` - but the contract is
still the artifact, not the sibling source tree. Do not import from `apps/api`.

```bash
make types    # fetches openapi.json, regenerates lib/api/schema.d.ts
```

Run this after any backend schema change. If `tsc` then fails, the contract
drifted — that is the mechanism working, not a nuisance to route around.

Backend base URL comes from `API_BASE_URL` (server-only). Auth is
`Authorization: Bearer <api_key>` from `LLMOBSERVE_API_KEY`, also server-only.

## Data model (mirrors backend)

- **Trace** — one user-facing request. id, name, user_id, session_id, tags,
  environment, release, metadata, timestamps. The list filters on every one
  of those except metadata; a tag filter is repeatable and conjunctive
- **Observation** — one LLM/tool call. Nestable via `parent_id`. model, input,
  output, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens
  (subsets of the two totals), latency_ms, cost_usd, prompt_name,
  prompt_version, metadata (provider facts on a shared vocabulary:
  finish_reason, tool_calls, time_to_first_token_ms, rate_limit, ...)
- **Score** — attached to a trace or observation. name, data_type
  (numeric | boolean | categorical), value (decimal string; booleans as 1/0),
  value_text, source, comment. Rendered from `TraceDetail.scores`

Observations form a tree. The detail view renders that nesting; do not flatten it.

## Routes (current phase)

```
app/
  (dash)/
    traces/page.tsx        # filterable, paginated list
    traces/[id]/page.tsx   # scores, nested span tree + payloads + metadata
lib/trace-filters.ts       # the one list of URL filters shared by page, form and proxy
```

## Design direction

Dense and legible, closer to a terminal or a log viewer than a marketing page.
Engineers scan this while debugging. Monospace for ids, models, and token counts.
Tabular numbers so columns align. Restrained color, used only to carry meaning —
latency and cost thresholds, error states — never for decoration.

## Conventions

- Node 20+, pnpm
- `tsc --noEmit` and `eslint` must both pass
- Components in `components/`, one per file, named exports
- No `any`. No `@ts-ignore` without a comment explaining why
- Dates always rendered in the user's local timezone, stored as UTC

## Commands

```bash
pnpm dev
pnpm build
pnpm test
pnpm lint
make types      # regenerate API types from backend openapi.json
```

## Build order (current phase)

1. Scaffold: Next.js 15 + TS strict + Tailwind + shadcn init
2. `lib/api/` — generated types, fetch wrapper, server-only auth
3. Route handler proxying to the backend so the key stays server-side
4. `traces/page.tsx` — table with pagination, filter by name and date range
5. `components/span-tree.tsx` — recursive nested observation renderer
6. `traces/[id]/page.tsx` — span tree, input/output payloads, cost and latency
7. Empty state with copy-paste SDK snippet for a project with no traces

Step 7 is the milestone: install fresh, send a trace from the SDK, see it here.

## Out of scope for now

Metrics dashboard, eval score views, prompt A/B comparison, auth/login flows,
multi-user accounts. They are in the architecture but not this phase.
