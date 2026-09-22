"use client";

import { useRef } from "react";

import type { Project } from "@/lib/api/types";

/**
 * Switch projects by posting to /auth/project, which re-checks membership
 * before storing the choice. Client-side only for submit-on-change; the form
 * still works with JavaScript off via the button.
 */
export function ProjectSwitcher({
  projects,
  current,
  next,
}: {
  projects: Project[];
  current: string;
  next: string;
}) {
  const form = useRef<HTMLFormElement>(null);
  if (projects.length < 2) {
    return <span className="font-mono text-xs text-muted-foreground">{projects[0]?.name}</span>;
  }
  return (
    <form ref={form} method="post" action="/auth/project" className="flex items-center gap-1">
      <input type="hidden" name="next" value={next} />
      <label className="sr-only" htmlFor="project">
        Project
      </label>
      <select
        id="project"
        name="project_id"
        defaultValue={current}
        onChange={() => form.current?.requestSubmit()}
        className="h-7 rounded border border-border bg-background px-1.5 font-mono text-xs"
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      <noscript>
        <button type="submit" className="h-7 rounded border border-border px-2 text-xs">
          Switch
        </button>
      </noscript>
    </form>
  );
}
