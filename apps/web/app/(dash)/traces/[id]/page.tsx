import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { SpanTree, TraceIdBadge } from "@/components/traces/span-tree";
import { ApiError, MissingApiConfigError, apiGet } from "@/lib/api";
import { projectHeader, requireContext } from "@/lib/auth/session";
import {
  formatCostUsd,
  formatDuration,
  formatTimestamp,
  formatTokens,
} from "@/lib/format";

export const dynamic = "force-dynamic";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function TraceDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  // An id that is not a UUID cannot name a trace, so it is a 404 here rather
  // than a round trip that comes back 422 and renders an error page under a
  // 200. Checked before the fetch so a crafted path costs no backend work.
  if (!UUID_PATTERN.test(id)) notFound();

  // Outside the try: redirect() works by throwing, and the catch below would
  // swallow a session-expiry redirect and render an error page instead.
  const { project } = await requireContext();

  let trace;
  try {
    // Path params are substituted and percent-encoded by the wrapper; the
    // contract makes `path` required for this operation, so a missing id is a
    // type error rather than a request to a literal "{trace_id}".
    trace = await apiGet("/v1/traces/{trace_id}", {
      path: { trace_id: id },
      headers: projectHeader(project),
    });
  } catch (error) {
    if (error instanceof ApiError && error.isUnauthorized) redirect("/auth/expired");
    // A trace in another project is a 404 from the API by design — it never
    // confirms that an id exists elsewhere — so this is the same page a
    // genuinely missing id gets.
    // 422 as well as 404: the backend validates the path parameter, and an id
    // it refuses names no trace either.
    if (error instanceof ApiError && (error.status === 404 || error.status === 422)) {
      notFound();
    }
    return <DetailFailure error={error} />;
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border px-4 py-3">
        <Link
          href="/traces"
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          ← All traces
        </Link>
        <div className="mt-2 flex flex-wrap items-baseline gap-3">
          <h1 className="text-sm font-semibold tracking-tight">
            {trace.name ?? <span className="italic text-muted-foreground">unnamed</span>}
          </h1>
          <TraceIdBadge id={trace.id} />
          <span className="font-mono text-xs text-muted-foreground">
            {formatTimestamp(trace.started_at)}
          </span>
        </div>

        <dl className="mt-3 flex flex-wrap gap-x-8 gap-y-2">
          <Stat label="Observations" value={String(trace.observation_count)} />
          <Stat label="Tokens" value={formatTokens(trace.total_tokens)} />
          <Stat
            label="Prompt / completion"
            value={`${formatTokens(trace.prompt_tokens)} / ${formatTokens(trace.completion_tokens)}`}
          />
          <Stat label="Cost" value={formatCostUsd(trace.total_cost_usd)} />
          <Stat
            label="Model time"
            value={formatDuration(trace.total_latency_ms)}
            hint="Sum of every observation's latency. Concurrent spans each count, so this exceeds the wall clock when calls run in parallel."
          />
          <Stat
            label="Duration"
            value={formatDuration(trace.duration_ms)}
            hint="Wall clock, from the trace's start to its end. Empty while the trace is still open."
          />
          {trace.user_id && <Stat label="User" value={trace.user_id} mono />}
          {trace.session_id && <Stat label="Session" value={trace.session_id} mono />}
        </dl>
      </header>

      {trace.observations.length === 0 ? (
        <div className="px-4 py-16 text-center">
          <h2 className="text-sm font-medium">No observations yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            This trace exists but none of its spans have arrived. It may still be
            running, or its observations may still be in flight.
          </p>
        </div>
      ) : (
        <SpanTree observations={trace.observations} />
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  mono,
}: {
  label: string;
  value: string;
  hint?: string;
  mono?: boolean;
}) {
  return (
    <div title={hint}>
      <dt className="text-[11px] uppercase tracking-wider text-muted-foreground">
        {label}
      </dt>
      <dd className={`text-xs tabular-nums ${mono ? "font-mono" : "font-mono"}`}>
        {value}
      </dd>
    </div>
  );
}

function DetailFailure({ error }: { error: unknown }) {
  const detail =
    error instanceof MissingApiConfigError
      ? `${error.variable} is not set. Copy .env.example to .env.local and fill it in.`
      : error instanceof ApiError && error.status === 403
        ? "This key authenticates but lacks the 'read' scope."
        : error instanceof ApiError
          ? error.message
          : "The backend returned an unexpected error.";

  return (
    <div className="min-h-screen bg-background px-4 py-16 text-foreground">
      <div className="mx-auto max-w-lg">
        <Link href="/traces" className="text-xs text-muted-foreground hover:text-foreground">
          ← All traces
        </Link>
        <h2 className="mt-4 text-sm font-medium text-destructive">
          Could not load this trace
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">{detail}</p>
      </div>
    </div>
  );
}
