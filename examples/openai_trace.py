#!/usr/bin/env python
"""Send one real OpenAI call through llm-metrics and watch its tokens and cost land.

Uses the SDK's OpenAI integration: `wrap_openai` instruments the client, so the
call site is unchanged and every completion becomes a `generation` observation
carrying the model, messages, response, token counts and latency. The server
prices it at write time from apps/api/app/services/pricing.py.

    # llm-metrics: an INGEST key from the dashboard (Settings -> API keys)
    export LLM_METRICS_API_KEY=llmo_sk_...
    # LLM_METRICS_HOST is optional: llm-metrics 0.2.0 defaults to the hosted API.
    export LLM_METRICS_DEBUG=1

    # OpenAI
    export OPENAI_API_KEY=sk-...

    pip install "llm-metrics[openai]>=0.2.0"
    python examples/openai_trace.py

Costs real money: one short request. Override the model with OPENAI_MODEL; it
must be in the server's pricebook or its cost will be recorded as unknown.
"""

import os
import sys

try:
    import llm_metrics
    from llm_metrics import observe
    from llm_metrics.integrations.openai import wrap_openai
    from openai import OpenAI
except ImportError as exc:
    sys.exit(f"{exc}\n  pip install 'llm-metrics[openai]>=0.2.0'")

if not os.environ.get("LLM_METRICS_API_KEY"):
    sys.exit("LLM_METRICS_API_KEY is not set. Use an ingest key from the dashboard.")
if not os.environ.get("LLM_METRICS_DEBUG"):
    print("! LLM_METRICS_DEBUG is unset - delivery failures will be silent.\n")

if not os.environ.get("OPENAI_API_KEY"):
    sys.exit(
        "OPENAI_API_KEY is not set. Create a key at\n"
        "  https://platform.openai.com/api-keys\n"
        "then:\n"
        "  export OPENAI_API_KEY=sk-..."
    )

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
QUESTION = "In two sentences: why do LLM observability tools record token counts?"

llm_metrics.configure(flush_at=1, flush_interval=0.5)
client = wrap_openai(OpenAI())


@observe(name="openai-smoke-test")
def main_trace() -> str:
    """The trace root; the wrapped completion nests under it."""
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": QUESTION}],
    )
    return completion.choices[0].message.content or ""


def main() -> None:
    print(f"llm-metrics {llm_metrics.__version__}")
    print(f"openai      {MODEL}\n")
    print(main_trace())

    llm_metrics.flush()
    stats = llm_metrics.stats()
    llm_metrics.shutdown()
    print(f"\ndelivered: {stats.sent_events} event(s), healthy={stats.healthy}")
    if stats.last_error:
        print(f"last error: {stats.last_error}")
    print("\nflushed. Open the dashboard: trace 'openai-smoke-test' should show")
    print("the model, input/output tokens, a cost, and the latency.")


if __name__ == "__main__":
    main()
