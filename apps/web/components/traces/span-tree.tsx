"use client";

import { useState } from "react";

import type { ObservationDetail, ScoreDetail } from "@/lib/api/types";
import {
  formatCostUsd,
  formatDuration,
  formatPercent,
  formatTokens,
  shortId,
} from "@/lib/format";
import { buildSpanTree, flattenTree, type SpanNode } from "@/lib/trace-tree";

import { ScoreBadges } from "./scores";

/**
 * The nested observation view.
 *
 * Client-side because a row expands to show its payloads; the data itself is
 * fetched and the tree is shaped on the server. Rows render as a flat list with
 * an indent, not as nested DOM, so that every row keeps the same column grid —
 * nesting <table>s or <div>s per level would stagger the numeric columns and
 * defeat the point of a scan-friendly view.
 */
export function SpanTree({
  observations,
  scores = [],
}: {
  observations: ObservationDetail[];
  scores?: ScoreDetail[];
}) {
  const rows = flattenTree(buildSpanTree(observations));
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const scoresByObservation = new Map<string, ScoreDetail[]>();
  for (const score of scores) {
    if (!score.observation_id) continue;
    const list = scoresByObservation.get(score.observation_id) ?? [];
    list.push(score);
    scoresByObservation.set(score.observation_id, list);
  }

  function toggle(id: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="divide-y divide-border/60">
      {rows.map((node) => (
        <SpanRow
          key={node.observation.id}
          node={node}
          scores={scoresByObservation.get(node.observation.id) ?? []}
          isExpanded={expanded.has(node.observation.id)}
          onToggle={() => toggle(node.observation.id)}
        />
      ))}
    </div>
  );
}

function SpanRow({
  node,
  scores,
  isExpanded,
  onToggle,
}: {
  node: SpanNode;
  scores: ScoreDetail[];
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const { observation, depth, orphaned } = node;
  const hasPayload = observation.input != null || observation.output != null;
  const hasDetails = hasPayload || hasTokenDetail(observation) || hasMetadata(observation);
  // Level carries the OpenTelemetry span status the SDK folds into it.
  const isError =
    observation.level != null &&
    ["error", "fatal", "critical"].includes(observation.level.toLowerCase());
  const cached = observation.cached_tokens ?? 0;
  const prompt = observation.prompt_tokens ?? 0;

  return (
    <div className={isError ? "bg-destructive/5" : undefined}>
      <button
        type="button"
        onClick={onToggle}
        disabled={!hasDetails}
        aria-expanded={hasDetails ? isExpanded : undefined}
        className="flex w-full items-center gap-2 px-4 py-2 text-left text-xs hover:bg-muted/40 disabled:cursor-default disabled:hover:bg-transparent"
      >
        {/* Indent is padding on a spacer, not nested markup, so the columns to
            the right stay on one grid regardless of depth. */}
        <span
          aria-hidden
          style={{ width: `${depth * 14}px` }}
          className="shrink-0"
        />
        <span className="w-3 shrink-0 font-mono text-muted-foreground">
          {hasDetails ? (isExpanded ? "▾" : "▸") : ""}
        </span>

        <span className="min-w-0 flex-1 truncate">
          <span className={isError ? "font-medium text-destructive" : "font-medium"}>
            {observation.name ?? (
              <span className="italic text-muted-foreground">unnamed</span>
            )}
          </span>
          <span className="ml-2 font-mono text-[11px] text-muted-foreground">
            {observation.type}
          </span>
          {observation.model && (
            <span className="ml-2 font-mono text-[11px] text-muted-foreground">
              {observation.model}
            </span>
          )}
          {observation.prompt_name && (
            <span
              title="Prompt template and version this call used"
              className="ml-2 rounded border border-border px-1 font-mono text-[10px] text-muted-foreground"
            >
              {observation.prompt_name}
              {observation.prompt_version ? `@${observation.prompt_version}` : ""}
            </span>
          )}
          {finishReason(observation) && finishReason(observation) !== "stop" && (
            <span
              title="Why generation stopped. 'length' means the reply was cut off."
              className={`ml-2 rounded border px-1 font-mono text-[10px] ${
                finishReason(observation) === "length"
                  ? "border-destructive/50 text-destructive"
                  : "border-border text-muted-foreground"
              }`}
            >
              {finishReason(observation)}
            </span>
          )}
          <ScoreBadges scores={scores} />
          {orphaned && (
            <span
              title="This span names a parent that is not in this trace yet — it may still be in flight."
              className="ml-2 rounded border border-border px-1 text-[10px] text-muted-foreground"
            >
              detached
            </span>
          )}
        </span>

        <span
          className="w-24 shrink-0 text-right font-mono tabular-nums text-muted-foreground"
          title={
            cached > 0
              ? `${formatTokens(cached)} of ${formatTokens(prompt)} prompt tokens served from cache (${formatPercent(cached, prompt)})`
              : undefined
          }
        >
          {observation.total_tokens ? formatTokens(observation.total_tokens) : "—"}
          {cached > 0 && (
            <span className="ml-1 text-[10px]">{formatPercent(cached, prompt)}⟲</span>
          )}
        </span>
        <span className="w-20 shrink-0 text-right font-mono tabular-nums text-muted-foreground">
          {observation.cost_usd ? formatCostUsd(observation.cost_usd) : "—"}
        </span>
        <span className="w-20 shrink-0 text-right font-mono tabular-nums">
          {formatDuration(observation.latency_ms)}
        </span>
      </button>

      {isExpanded && hasPayload && (
        <div className="grid gap-3 px-4 pb-3 pl-10 md:grid-cols-2">
          <Payload label="Input" value={observation.input} />
          <Payload label="Output" value={observation.output} />
        </div>
      )}
      {isExpanded && (hasTokenDetail(observation) || hasMetadata(observation)) && (
        <Details observation={observation} />
      )}
      {isExpanded && observation.status_message && (
        <p className="px-4 pb-3 pl-10 font-mono text-[11px] text-destructive">
          {observation.status_message}
        </p>
      )}
    </div>
  );
}

function hasTokenDetail(observation: ObservationDetail): boolean {
  return Boolean(observation.cached_tokens || observation.reasoning_tokens);
}

function hasMetadata(observation: ObservationDetail): boolean {
  return Object.keys(observation.metadata).length > 0;
}

function finishReason(observation: ObservationDetail): string | null {
  const value = observation.metadata["finish_reason"];
  return typeof value === "string" ? value : null;
}

/**
 * The metadata keys worth naming, in the order an engineer reads them: what
 * the call did, how fast it started, and what the provider said about it.
 * Everything else in metadata follows in key order.
 */
const LEADING_KEYS = [
  "finish_reason",
  "refusal",
  "tool_calls",
  "tools",
  "time_to_first_token_ms",
  "output_tokens_per_second",
  "stream_completed",
  "request_id",
  "http_status",
  "http_attempts",
  "upstream_processing_ms",
  "rate_limit",
  "system_fingerprint",
  "service_tier",
] as const;

function Details({ observation }: { observation: ObservationDetail }) {
  const entries = orderedMetadata(observation.metadata);
  const cached = observation.cached_tokens ?? 0;
  const reasoning = observation.reasoning_tokens ?? 0;

  return (
    <dl className="grid gap-x-6 gap-y-1 px-4 pb-3 pl-10 font-mono text-[11px] sm:grid-cols-[max-content_1fr]">
      {cached > 0 && (
        <Row label="cached tokens">
          {formatTokens(cached)} of {formatTokens(observation.prompt_tokens ?? 0)} prompt (
          {formatPercent(cached, observation.prompt_tokens ?? 0)})
        </Row>
      )}
      {reasoning > 0 && (
        <Row label="reasoning tokens">
          {formatTokens(reasoning)} of {formatTokens(observation.completion_tokens ?? 0)}{" "}
          completion, hidden from the caller
        </Row>
      )}
      {entries.map(([key, value]) => (
        <Row key={key} label={key}>
          {renderValue(key, value)}
        </Row>
      ))}
    </dl>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </>
  );
}

function orderedMetadata(metadata: Record<string, unknown>): [string, unknown][] {
  const leading: [string, unknown][] = [];
  const rest: [string, unknown][] = [];
  const known = new Set<string>(LEADING_KEYS);
  for (const key of LEADING_KEYS) {
    if (key in metadata) leading.push([key, metadata[key]]);
  }
  for (const key of Object.keys(metadata).sort()) {
    if (!known.has(key)) rest.push([key, metadata[key]]);
  }
  return [...leading, ...rest];
}

function renderValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (key.endsWith("_ms")) return formatDuration(value);
    return String(value);
  }
  if (typeof value === "string" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
    return value.join(", ");
  }
  return JSON.stringify(value);
}

function Payload({ label, value }: { label: string; value: unknown }) {
  if (value == null) return null;
  // Strings are shown as-is; anything else is JSONB and reads better formatted.
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[11px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <pre className="max-h-64 overflow-auto rounded border border-border bg-muted/30 p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
        {text}
      </pre>
    </div>
  );
}

/** Small helper reused by the detail header. */
export function TraceIdBadge({ id }: { id: string }) {
  return (
    <span title={id} className="font-mono text-xs text-muted-foreground">
      {shortId(id)}
    </span>
  );
}
