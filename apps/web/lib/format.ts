/**
 * Display formatters for the trace table.
 *
 * Shared rather than inlined because the same value must read identically in
 * the list and the detail view — a cost that renders as "$0.0004" in one place
 * and "$0.00" in the other looks like a bug in the data.
 */

/** Thousands separators plus tabular figures, so columns align when stacked. */
const INTEGER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

export function formatTokens(value: number): string {
  return INTEGER.format(value);
}

/**
 * Render a cost that arrived as a decimal string.
 *
 * Kept in string space until the last moment. Sub-cent costs are the normal
 * case for a single call, so a flat 2-decimal format would show "$0.00" for
 * most rows and make the column useless; below a cent we widen to significant
 * digits instead of rounding the value away.
 */
export function formatCostUsd(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  if (amount === 0) return "$0";
  if (amount < 0.01) {
    // 4 significant digits keeps $0.0001234 legible without a wall of zeros.
    return `$${amount.toPrecision(4).replace(/0+$/, "").replace(/\.$/, "")}`;
  }
  return `$${amount.toFixed(amount < 1 ? 4 : 2)}`;
}

/** Durations in the unit an engineer scanning for slow calls actually wants. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${INTEGER.format(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

/**
 * Absolute local time. The backend stores and sends UTC; the reader thinks in
 * their own timezone, so the conversion happens here and only here.
 */
export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** "3m ago" — the form that answers "is this live?" at a glance. */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const elapsed = now - new Date(iso).getTime();
  if (!Number.isFinite(elapsed)) return "—";
  const seconds = Math.round(elapsed / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** First 8 characters of a UUID — enough to recognise, short enough to scan. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}
