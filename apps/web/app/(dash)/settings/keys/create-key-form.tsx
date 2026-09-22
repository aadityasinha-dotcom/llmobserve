"use client";

import { useActionState } from "react";

import { CopyableSnippet } from "@/components/traces/copyable-snippet";

import { createKeyAction, type CreateKeyState } from "./actions";

const INITIAL: CreateKeyState = { status: "idle" };

export function CreateKeyForm({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [state, action, pending] = useActionState(createKeyAction, INITIAL);

  return (
    <div>
      <form action={action} className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-[11px] uppercase tracking-wider text-muted-foreground">Label</span>
          <input
            name="label"
            placeholder="sdk, ci, staging…"
            maxLength={255}
            className="h-8 w-48 rounded border border-border bg-background px-2 font-mono text-xs"
          />
        </label>
        <fieldset className="flex flex-col gap-1">
          <legend className="text-[11px] uppercase tracking-wider text-muted-foreground">Scopes</legend>
          <div className="flex h-8 items-center gap-3 text-xs">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" name="scopes" value="ingest" defaultChecked /> ingest
            </label>
            <label className="flex items-center gap-1.5">
              <input type="checkbox" name="scopes" value="read" /> read
            </label>
          </div>
        </fieldset>
        <button
          type="submit"
          disabled={pending}
          className="h-8 rounded bg-foreground px-3 text-xs font-medium text-background hover:opacity-90 disabled:opacity-50"
        >
          {pending ? "Creating…" : "Create key"}
        </button>
      </form>
      <p className="mt-2 text-[11px] text-muted-foreground">
        An SDK needs only <span className="font-mono">ingest</span>. Leave{" "}
        <span className="font-mono">read</span> off unless the key must also read traces back:
        a key that ships inside an application should not be able to read every stored prompt.
      </p>

      {state.status === "error" && (
        <p role="alert" className="mt-3 text-xs text-destructive">
          {state.message}
        </p>
      )}

      {state.status === "created" && (
        <div className="mt-4 rounded border border-border bg-muted/30 p-3">
          <p className="text-xs font-medium">
            Copy this key now. It will not be shown again — only its hash is stored.
          </p>
          <CopyableSnippet code={state.apiKey} />
          {state.scopes.includes("ingest") && (
            <>
              <p className="mt-3 text-xs text-muted-foreground">Point the SDK at it:</p>
              <CopyableSnippet
                code={`pip install llm-metrics

export LLM_METRICS_API_KEY=${state.apiKey}
export LLM_METRICS_HOST=${apiBaseUrl}`}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}
