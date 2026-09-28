#!/usr/bin/env python
"""Exercise every llm-metrics 0.2.0 feature end to end. No LLM call, no cost.

Sends one trace built from the SDK's public API only, then reads the SDK's own
delivery counters to prove it arrived:

    configure(environment=, release=)       deploy stamping on the trace
    update_trace(user_id, session_id, tags) attribution from inside a call
    update_observation(model, *_tokens,     token detail and prompt versioning
                       prompt_name/version)
    score(...)                              numeric, boolean and categorical
    stats()                                 the pipeline's health counters

Install the RELEASED package in a clean environment - that is what users get,
and it is the thing worth testing, not the repo checkout:

    python3 -m venv /tmp/llmm && /tmp/llmm/bin/pip install "llm-metrics>=0.2.1"

    export LLM_METRICS_API_KEY=llmo_sk_...   # an INGEST key from the dashboard
    export LLM_METRICS_DEBUG=1
    # LLM_METRICS_HOST is optional: 0.2.0 defaults to the hosted API.

    /tmp/llmm/bin/python examples/release_check.py

Exit status is 0 only if the SDK reports every event delivered.
"""

import inspect
import os
import sys
import time
import uuid

try:
    import llm_metrics
    from llm_metrics import observe, score, update_observation, update_trace
except ImportError as exc:
    sys.exit(f"llm-metrics is not importable ({exc}).\n  pip install 'llm-metrics==0.2.0'")

if not os.environ.get("LLM_METRICS_API_KEY"):
    sys.exit("LLM_METRICS_API_KEY is not set. Use an ingest key from the dashboard.")

# A fresh id per run, so this run's rows are easy to find and cannot be
# confused with an earlier one's.
RUN = uuid.uuid4().hex[:8]

# flush(timeout=) arrived in 0.2.1 together with the delivery guarantee.
SDK_HAS_FLUSH_TIMEOUT = "timeout" in inspect.signature(llm_metrics.flush).parameters

llm_metrics.configure(
    flush_at=1,
    flush_interval=0.5,
    environment="release-check",
    release=f"llm-metrics-{llm_metrics.__version__}",
)


@observe(name="summarise", as_type="generation")
def summarise(text: str) -> str:
    """Stands in for a model call, reporting the usage a real one would.

    Numbers chosen so the server's cost is checkable by hand, at Claude Haiku
    4.5's rates ($1/M input, $0.10/M cache reads, $5/M output):
        400 fresh input  x $1.00  = $0.00040
        800 cached input x $0.10  = $0.00008
        300 output       x $5.00  = $0.00150
                                    --------
                                    $0.00198
    cached_tokens and reasoning_tokens are SUBSETS of the two totals, never
    additions to them.
    """
    update_observation(
        model="claude-haiku-4-5",
        prompt_tokens=1200,
        cached_tokens=800,
        completion_tokens=300,
        reasoning_tokens=120,
        prompt_name="summarise-v",
        prompt_version=3,
    )
    return text[:40] + "..."


@observe(name="release-check")
def handle(question: str) -> str:
    update_trace(
        user_id=f"user-{RUN}",
        session_id=f"session-{RUN}",
        tags=["release-check", f"run-{RUN}"],
        metadata={"sdk_version": llm_metrics.__version__},
    )
    answer = summarise(question)
    # Scores attach to the ambient trace. One of each data type.
    score("helpfulness", 0.9, source="llm_judge", comment="release check")
    score("contains_citation", False, source="heuristic")
    score("tone", "neutral", source="human")
    return answer


def main() -> None:
    print(f"llm-metrics {llm_metrics.__version__} from {llm_metrics.__file__}")
    print(f"run id      {RUN}\n")

    handle("Why do LLM observability tools record token counts at all?")

    # 0.2.1+: flush() blocks until nothing is queued or in flight and returns
    # True on delivery. 0.2.0 returned None and could come back with the last
    # batch still on the wire, so on that version fall back to watching the
    # SDK's own counters until everything queued has been flushed.
    delivered = llm_metrics.flush(timeout=10) if SDK_HAS_FLUSH_TIMEOUT else None
    stats = llm_metrics.stats()
    if delivered is None:
        deadline = time.monotonic() + 10
        while stats.flushed < stats.queued and time.monotonic() < deadline:
            time.sleep(0.1)
            stats = llm_metrics.stats()
    llm_metrics.shutdown()

    print(f"sent events       {stats.sent_events}")
    print(f"failed batches    {stats.failed_batches}")
    print(f"dropped           {stats.dropped_by_transport + stats.dropped_on_overflow}")
    if stats.last_error:
        print(f"last error        {stats.last_error}")
    print(f"healthy           {stats.healthy}\n")

    # 1 trace + 2 observations + 3 scores.
    dropped = stats.dropped_by_transport + stats.dropped_on_overflow
    if delivered is False:
        sys.exit("FAILED: flush() timed out with events still in flight.")
    if not stats.healthy or dropped or stats.failed_batches or stats.sent_events < 6:
        sys.exit("FAILED: the SDK did not deliver everything. Re-run with LLM_METRICS_DEBUG=1.")

    print("OK. In the dashboard, filter by tag", f"run-{RUN}", "and check:")
    print("  trace 'release-check'  user, session, tags, environment=release-check,")
    print(f"                         release=llm-metrics-{llm_metrics.__version__}")
    print("  generation 'summarise' claude-haiku-4-5, 1200 in (800 cached) / 300 out")
    print("                         (120 reasoning), prompt summarise-v v3, cost $0.00198")
    print("  scores                 helpfulness 0.9, contains_citation false, tone neutral")


if __name__ == "__main__":
    main()
