"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

/**
 * Name and date-range filters.
 *
 * The only client component on this page — everything else renders on the
 * server. It owns no data, just the form state, and navigates by pushing a new
 * URL so the server component re-runs the query. That keeps the filter state in
 * the URL, which means a filtered view is shareable and survives a reload.
 *
 * Submitting always drops the cursor trail. A cursor is a position in one
 * specific ordering; carrying it across a filter change would resume from a row
 * that may no longer be in the result set.
 */
export function TraceFilters() {
  const router = useRouter();
  const params = useSearchParams();

  const [name, setName] = useState(params.get("name") ?? "");
  const [from, setFrom] = useState(params.get("from") ?? "");
  const [to, setTo] = useState(params.get("to") ?? "");

  function submit(event: FormEvent) {
    event.preventDefault();
    const next = new URLSearchParams();
    if (name.trim()) next.set("name", name.trim());
    // A datetime-local value is local wall time with no offset. Converting here
    // means "since 9am" means 9am where the reader is, not 9am UTC.
    if (from) next.set("from", new Date(from).toISOString());
    if (to) next.set("to", new Date(to).toISOString());
    router.push(next.toString() ? `/traces?${next}` : "/traces");
  }

  function clear() {
    setName("");
    setFrom("");
    setTo("");
    router.push("/traces");
  }

  const isFiltered = Boolean(name || from || to);

  return (
    <form
      onSubmit={submit}
      className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3"
    >
      <label className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          Name
        </span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="exact match"
          className="h-8 w-48 rounded border border-border bg-background px-2 font-mono text-xs outline-none focus:border-foreground/40"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          From
        </span>
        <input
          type="datetime-local"
          value={from}
          onChange={(event) => setFrom(event.target.value)}
          className="h-8 rounded border border-border bg-background px-2 font-mono text-xs outline-none focus:border-foreground/40"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          To
        </span>
        <input
          type="datetime-local"
          value={to}
          onChange={(event) => setTo(event.target.value)}
          className="h-8 rounded border border-border bg-background px-2 font-mono text-xs outline-none focus:border-foreground/40"
        />
      </label>

      <button
        type="submit"
        className="h-8 rounded bg-foreground px-3 text-xs font-medium text-background hover:opacity-90"
      >
        Apply
      </button>
      {isFiltered && (
        <button
          type="button"
          onClick={clear}
          className="h-8 rounded border border-border px-3 text-xs text-muted-foreground hover:text-foreground"
        >
          Clear
        </button>
      )}
    </form>
  );
}
