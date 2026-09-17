"use client";

import { useState } from "react";

import type { ObservationDetail } from "@/lib/api/types";
import {
  formatCostUsd,
  formatDuration,
  formatTokens,
  shortId,
} from "@/lib/format";
import { buildSpanTree, flattenTree, type SpanNode } from "@/lib/trace-tree";

/**
 * The nested observation view.
 *
 * Client-side because a row expands to show its payloads; the data itself is
 * fetched and the tree is shaped on the server. Rows render as a flat list with
 * an indent, not as nested DOM, so that every row keeps the same column grid —
 * nesting <table>s or <div>s per level would stagger the numeric columns and
 * defeat the point of a scan-friendly view.
 */
export function SpanTree({ observations }: { observations: ObservationDetail[] }) {
  const rows = flattenTree(buildSpanTree(observations));
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

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
          isExpanded={expanded.has(node.observation.id)}
          onToggle={() => toggle(node.observation.id)}
        />
      ))}
    </div>
  );
}

function SpanRow({
  node,
  isExpanded,
  onToggle,
}: {
  node: SpanNode;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const { observation, depth, orphaned } = node;
  const hasPayload = observation.input != null || observation.output != null;
  // Level carries the OpenTelemetry span status the SDK folds into it.
  const isError =
    observation.level != null &&
    ["error", "fatal", "critical"].includes(observation.level.toLowerCase());

  return (
    <div className={isError ? "bg-destructive/5" : undefined}>
      <button
        type="button"
        onClick={onToggle}
        disabled={!hasPayload}
        aria-expanded={hasPayload ? isExpanded : undefined}
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
          {hasPayload ? (isExpanded ? "▾" : "▸") : ""}
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
          {orphaned && (
            <span
              title="This span names a parent that is not in this trace yet — it may still be in flight."
              className="ml-2 rounded border border-border px-1 text-[10px] text-muted-foreground"
            >
              detached
            </span>
          )}
        </span>

        <span className="w-20 shrink-0 text-right font-mono tabular-nums text-muted-foreground">
          {observation.total_tokens ? formatTokens(observation.total_tokens) : "—"}
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
      {isExpanded && observation.status_message && (
        <p className="px-4 pb-3 pl-10 font-mono text-[11px] text-destructive">
          {observation.status_message}
        </p>
      )}
    </div>
  );
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
