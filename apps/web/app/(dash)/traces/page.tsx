import Link from "next/link";
import { Suspense } from "react";

import { NoMatchingTraces, NoTracesYet } from "@/components/traces/empty-state";
import { TraceFilters } from "@/components/traces/trace-filters";
import { ApiError, MissingApiConfigError, apiGet } from "@/lib/api";
import type { TraceListItem } from "@/lib/api/types";
import {
  formatCostUsd,
  formatDuration,
  formatRelative,
  formatTimestamp,
  formatTokens,
  shortId,
} from "@/lib/format";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;
// Cap on the cursor trail carried in the URL. Keyset pagination has no concept
// of "page N-1", so going back means remembering where each page began; past
// this depth the Back button stops rather than the URL growing without bound.
const MAX_TRAIL = 50;

type SearchParams = Record<string, string | string[] | undefined>;

function one(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[value.length - 1];
  return value ?? undefined;
}

/** The cursor trail: one entry per page visited, current page last. */
function trailOf(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return (Array.isArray(value) ? value : [value]).slice(-MAX_TRAIL);
}

function hrefWith(base: SearchParams, trail: string[]): string {
  const params = new URLSearchParams();
  for (const key of ["name", "from", "to"] as const) {
    const value = one(base[key]);
    if (value) params.set(key, value);
  }
  for (const cursor of trail) params.append("cursor", cursor);
  const query = params.toString();
  return query ? `/traces?${query}` : "/traces";
}

export default async function TracesPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border px-4 py-3">
        <h1 className="text-sm font-semibold tracking-tight">Traces</h1>
      </header>
      <TraceFilters />
      {/*
        The Suspense boundary is here rather than in a segment loading.tsx on
        purpose. A loading.tsx at this level would also wrap /traces/[id], and
        the shell it flushes commits the response as 200 before that page can
        call notFound() - so a missing trace would render the not-found UI under
        a 200 status. Scoping the boundary to the table keeps the detail route
        unbuffered and able to answer 404 properly.
      */}
      <Suspense key={JSON.stringify(params)} fallback={<TableSkeleton />}>
        <TraceTableSection params={params} />
      </Suspense>
    </div>
  );
}

async function TraceTableSection({ params }: { params: SearchParams }) {
  const name = one(params.name);
  const from = one(params.from);
  const to = one(params.to);
  const trail = trailOf(params.cursor);
  const isFiltered = Boolean(name || from || to);

  let page;
  try {
    page = await apiGet("/v1/traces", {
      query: {
        limit: PAGE_SIZE,
        // Undefined keys are dropped by the fetch wrapper, so an absent filter
        // is genuinely absent rather than an empty-string match.
        name,
        from,
        to,
        cursor: trail.at(-1),
      },
    });
  } catch (error) {
    return <LoadFailure error={error} />;
  }

  const traces = page.data as TraceListItem[];

  return (
    <>
      <div className="flex items-baseline justify-between px-4 py-2 text-xs text-muted-foreground">
        <span className="font-mono tabular-nums">
          {traces.length} shown{page.has_more ? " · more available" : ""}
        </span>
      </div>

      {traces.length === 0 ? (
        isFiltered ? (
          <NoMatchingTraces />
        ) : (
          <NoTracesYet />
        )
      ) : (
        <TraceTable traces={traces} />
      )}

      <Pagination
        base={params}
        trail={trail}
        nextCursor={page.next_cursor ?? null}
        hasMore={page.has_more}
      />
    </>
  );
}

/**
 * Shaped like the table it stands in for - same row height, same column count -
 * so content does not jump when it arrives. Required, not polish: the query
 * crosses the network to Postgres and a blank panel reads as a broken page.
 */
function TableSkeleton() {
  return (
    <div className="divide-y divide-border/60" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading traces…</span>
      {Array.from({ length: 12 }).map((_, index) => (
        <div key={index} className="flex items-center gap-4 px-4 py-2.5">
          <div className="h-3 w-48 animate-pulse rounded bg-muted" />
          <div className="ml-auto h-3 w-16 animate-pulse rounded bg-muted" />
          <div className="h-3 w-12 animate-pulse rounded bg-muted" />
          <div className="h-3 w-16 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}

function TraceTable({ traces }: { traces: TraceListItem[] }) {
  return (
    // The table scrolls inside its own container rather than letting the page
    // scroll sideways, which is what keeps the header and filters in place.
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-border text-left text-[11px] uppercase tracking-wider text-muted-foreground">
            <th className="px-4 py-2 font-medium">Trace</th>
            <th className="px-3 py-2 font-medium">Started</th>
            <th className="px-3 py-2 text-right font-medium">Obs</th>
            <th className="px-3 py-2 text-right font-medium">Tokens</th>
            <th className="px-3 py-2 text-right font-medium">Cost</th>
            <th className="px-3 py-2 text-right font-medium">Model time</th>
            <th className="px-3 py-2 text-right font-medium">Duration</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((trace) => (
            <tr
              key={trace.id}
              className="border-b border-border/60 hover:bg-muted/40"
            >
              <td className="px-4 py-2">
                <Link href={`/traces/${trace.id}`} className="group block">
                  <span className="font-medium group-hover:underline">
                    {trace.name ?? (
                      <span className="text-muted-foreground italic">
                        unnamed
                      </span>
                    )}
                  </span>
                  <span className="ml-2 font-mono text-[11px] text-muted-foreground">
                    {shortId(trace.id)}
                  </span>
                </Link>
              </td>
              <td className="px-3 py-2 whitespace-nowrap">
                <span title={formatTimestamp(trace.started_at)}>
                  {formatRelative(trace.started_at)}
                </span>
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">
                {trace.observation_count}
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">
                {formatTokens(trace.total_tokens)}
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">
                {formatCostUsd(trace.total_cost_usd)}
              </td>
              {/* Summed observation latency: model time spent, which double
                  counts concurrent spans and is NOT the trace's wall clock. */}
              <td className="px-3 py-2 text-right font-mono tabular-nums text-muted-foreground">
                {formatDuration(trace.total_latency_ms)}
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">
                {formatDuration(trace.duration_ms)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Pagination({
  base,
  trail,
  nextCursor,
  hasMore,
}: {
  base: SearchParams;
  trail: string[];
  nextCursor: string | null;
  hasMore: boolean;
}) {
  const canGoBack = trail.length > 0;
  if (!canGoBack && !hasMore) return null;

  return (
    <nav className="flex items-center justify-between px-4 py-3 text-xs">
      {canGoBack ? (
        <Link
          href={hrefWith(base, trail.slice(0, -1))}
          className="rounded border border-border px-3 py-1.5 hover:bg-muted/60"
        >
          ← Previous
        </Link>
      ) : (
        <span />
      )}
      <span className="font-mono text-muted-foreground tabular-nums">
        page {trail.length + 1}
      </span>
      {hasMore && nextCursor ? (
        <Link
          href={hrefWith(base, [...trail, nextCursor])}
          className="rounded border border-border px-3 py-1.5 hover:bg-muted/60"
        >
          Next →
        </Link>
      ) : (
        <span />
      )}
    </nav>
  );
}

/**
 * Failures the reader can act on, separated from ones they cannot.
 *
 * A 401 and an unreachable backend need different fixes, and both are far more
 * likely during setup than a genuine server fault — so each says what to check
 * rather than rendering one generic error.
 */
function LoadFailure({ error }: { error: unknown }) {
  let title = "Could not load traces";
  let detail = "The backend returned an unexpected error.";

  if (error instanceof MissingApiConfigError) {
    title = "Dashboard is not configured";
    detail = `${error.variable} is not set. Copy .env.example to .env.local and fill it in.`;
  } else if (error instanceof ApiError && error.isUnauthorized) {
    title = "API key rejected";
    detail =
      "LLMOBSERVE_API_KEY is not a key this backend recognises. Issue one with `make key NAME=...`.";
  } else if (error instanceof ApiError && error.status === 403) {
    // A valid key without the "read" scope. Distinct from 401 on purpose:
    // replacing the key with another ingest key would not help, so the message
    // has to name the scope rather than say "rejected".
    title = "API key cannot read traces";
    detail =
      "This key authenticates but lacks the 'read' scope. Issue one with: make key NAME=<project> ARGS=\"--add --scopes read\"";
  } else if (error instanceof ApiError && error.status === 0) {
    title = "Backend unreachable";
    detail = `${error.message} Check that the API is running and API_BASE_URL points at it.`;
  } else if (error instanceof ApiError) {
    detail = error.message;
  }

  return (
    <div className="min-h-screen bg-background px-4 py-16 text-foreground">
      <div className="mx-auto max-w-lg">
        <h2 className="text-sm font-medium text-destructive">{title}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{detail}</p>
      </div>
    </div>
  );
}
