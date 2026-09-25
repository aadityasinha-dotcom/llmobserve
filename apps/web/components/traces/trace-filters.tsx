"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { parseTagInput, writeFilters, type TraceFilterValues } from "@/lib/trace-filters";

/**
 * The trace-list filters: who, where, and when.
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
  const [userId, setUserId] = useState(params.get("user_id") ?? "");
  const [sessionId, setSessionId] = useState(params.get("session_id") ?? "");
  const [environment, setEnvironment] = useState(params.get("environment") ?? "");
  const [release, setRelease] = useState(params.get("release") ?? "");
  const [tags, setTags] = useState(params.getAll("tag").join(", "));
  const [from, setFrom] = useState(params.get("from") ?? "");
  const [to, setTo] = useState(params.get("to") ?? "");

  function submit(event: FormEvent) {
    event.preventDefault();
    const values: TraceFilterValues = {
      name: name.trim() || undefined,
      user_id: userId.trim() || undefined,
      session_id: sessionId.trim() || undefined,
      environment: environment.trim() || undefined,
      release: release.trim() || undefined,
      tags: parseTagInput(tags),
      // A datetime-local value is local wall time with no offset. Converting
      // here means "since 9am" means 9am where the reader is, not 9am UTC.
      from: from ? new Date(from).toISOString() : undefined,
      to: to ? new Date(to).toISOString() : undefined,
    };
    const next = new URLSearchParams();
    writeFilters(values, next);
    router.push(next.toString() ? `/traces?${next}` : "/traces");
  }

  function clear() {
    setName("");
    setUserId("");
    setSessionId("");
    setEnvironment("");
    setRelease("");
    setTags("");
    setFrom("");
    setTo("");
    router.push("/traces");
  }

  const isFiltered = Boolean(
    name || userId || sessionId || environment || release || tags || from || to,
  );

  return (
    <form
      onSubmit={submit}
      className="flex flex-wrap items-end gap-3 border-b border-border px-4 py-3"
    >
      <Field label="Name" value={name} onChange={setName} placeholder="exact match" width="w-40" />
      <Field label="User" value={userId} onChange={setUserId} placeholder="user_id" width="w-32" />
      <Field
        label="Session"
        value={sessionId}
        onChange={setSessionId}
        placeholder="session_id"
        width="w-32"
      />
      <Field
        label="Env"
        value={environment}
        onChange={setEnvironment}
        placeholder="prod"
        width="w-24"
      />
      <Field label="Release" value={release} onChange={setRelease} placeholder="sha" width="w-28" />
      <Field
        label="Tags"
        value={tags}
        onChange={setTags}
        placeholder="beta, qa"
        width="w-36"
        hint="Comma separated. A trace must carry every tag listed."
      />

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

function Field({
  label,
  value,
  onChange,
  placeholder,
  width,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  width: string;
  hint?: string;
}) {
  return (
    <label className="flex flex-col gap-1" title={hint}>
      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className={`h-8 ${width} rounded border border-border bg-background px-2 font-mono text-xs outline-none focus:border-foreground/40`}
      />
    </label>
  );
}
