import { CopyableSnippet } from "./copyable-snippet";

const SNIPPET = `pip install llmobserve

import llmobserve

llmobserve.init(
    api_key="<your project key>",
    base_url="http://localhost:8000",
)

with llmobserve.trace(name="hello"):
    ...  # your LLM call here`;

/**
 * Shown when a project has no traces at all.
 *
 * Deliberately not a shrug. A fresh install reaching this screen has done
 * nothing wrong — it has nothing to show yet — so the empty state's job is to
 * close the loop that gets the first trace in, which is why the SDK snippet is
 * here rather than in a doc the reader would have to go find.
 */
export function NoTracesYet() {
  return (
    <div className="px-4 py-16">
      <div className="mx-auto max-w-lg">
        <h2 className="text-sm font-medium">No traces yet</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Nothing has been ingested for this project. Send one from the SDK and
          it will appear here.
        </p>
        <CopyableSnippet code={SNIPPET} />
        <p className="mt-3 text-xs text-muted-foreground">
          Already sending traces? Check that{" "}
          <code className="font-mono">API_BASE_URL</code> and{" "}
          <code className="font-mono">LLMOBSERVE_API_KEY</code> in{" "}
          <code className="font-mono">.env.local</code> point at the same project
          the SDK is writing to.
        </p>
      </div>
    </div>
  );
}

/**
 * Shown when filters are active but match nothing. Distinct from NoTracesYet on
 * purpose: "you have no data" and "your filter excluded everything" call for
 * opposite next actions, and conflating them sends someone debugging their SDK
 * when all they need to do is widen a date range.
 */
export function NoMatchingTraces() {
  return (
    <div className="px-4 py-16 text-center">
      <h2 className="text-sm font-medium">No traces match these filters</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Try widening the date range, or clearing the name filter.
      </p>
    </div>
  );
}
