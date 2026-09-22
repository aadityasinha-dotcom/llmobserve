import { CopyableSnippet } from "./copyable-snippet";

const SNIPPET = `pip install llm-metrics

# export LLM_METRICS_API_KEY=<an ingest key from Settings → API keys>
# export LLM_METRICS_HOST=<this deployment's API URL>
from llm_metrics import observe

@observe(name="hello")
def handle(question: str) -> str:
    ...  # your LLM call here

handle("hi")`;

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
          Need a key? Create one with the <span className="font-mono">ingest</span> scope under{" "}
          <a href="/settings/keys" className="underline">API keys</a>. Already sending traces?
          Check that the SDK&apos;s key belongs to the project selected above.
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
