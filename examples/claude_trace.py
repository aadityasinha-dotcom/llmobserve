#!/usr/bin/env python
"""Send one real Claude call through llm-metrics and watch its tokens and cost land.

Uses the SDK's Anthropic integration (llm-metrics 0.2.0+): `wrap_anthropic`
instruments the client, so the call site is unchanged and the message becomes a
`generation` observation with the model, messages, reply, token counts - cache
reads broken out as `cached_tokens` - latency, and the response headers
(request id, rate-limit headroom). The server prices it at write time.

    pip install "llm-metrics[anthropic]>=0.2.0"

    export LLM_METRICS_API_KEY=llmo_sk_...   # an INGEST key from the dashboard
    export LLM_METRICS_DEBUG=1
    # LLM_METRICS_HOST is optional: 0.2.0 defaults to the hosted API.

    export ANTHROPIC_API_KEY=sk-ant-...

    python examples/claude_trace.py

Costs real money: one short request. Override the model with CLAUDE_MODEL.

No refusal fallbacks here, deliberately. Anthropic recommends
`fallbacks: "default"` on Claude Opus 5, but that is a beta feature on
`client.beta.messages`, and wrap_anthropic in 0.2.0 instruments only
`client.messages` - a beta call would go untraced, silently. Until the SDK
covers the beta namespace, this example stays on the path it can see.
"""

import os
import sys

try:
    import anthropic
    import llm_metrics
    from llm_metrics import observe
    from llm_metrics.integrations.anthropic import wrap_anthropic
except ImportError as exc:
    sys.exit(f"{exc}\n  pip install 'llm-metrics[anthropic]>=0.2.0'")

if not os.environ.get("LLM_METRICS_API_KEY"):
    sys.exit("LLM_METRICS_API_KEY is not set. Use an ingest key from the dashboard.")
if not os.environ.get("LLM_METRICS_DEBUG"):
    print("! LLM_METRICS_DEBUG is unset - delivery failures will be silent.\n")

# The Anthropic SDK also accepts ANTHROPIC_AUTH_TOKEN or a saved CLI profile, so
# only refuse when none of the three is present - and say so plainly, instead
# of letting the SDK raise a TypeError from deep inside the first request.
if not (
    os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or os.path.isdir(os.path.expanduser("~/.config/anthropic"))
):
    sys.exit(
        "No Anthropic credentials found. Create a key at\n"
        "  https://console.anthropic.com/settings/keys\n"
        "then:\n"
        "  export ANTHROPIC_API_KEY=sk-ant-..."
    )

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
QUESTION = "In two sentences: why do LLM observability tools record token counts?"

# Deliver each event as soon as it closes; right for a smoke test.
llm_metrics.configure(flush_at=1, flush_interval=0.5)
client = wrap_anthropic(anthropic.Anthropic())


@observe(name="claude-smoke-test")
def main_trace() -> str:
    """The trace root; the wrapped message nests under it."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": QUESTION}],
    )
    if response.stop_reason == "refusal":
        return "(Claude declined this request)"
    return "".join(block.text for block in response.content if block.type == "text")


def main() -> None:
    print(f"llm-metrics {llm_metrics.__version__}")
    print(f"claude      {MODEL}\n")
    print(main_trace())

    # The buffer delivers on a background thread; wait for it before exiting.
    llm_metrics.flush()
    stats = llm_metrics.stats()
    llm_metrics.shutdown()
    print(f"\ndelivered: {stats.sent_events} event(s), healthy={stats.healthy}")
    if stats.last_error:
        print(f"last error: {stats.last_error}")
    print("Open the dashboard: trace 'claude-smoke-test' shows the model, tokens,")
    print("cost, latency, and the request id Anthropic assigned.")


if __name__ == "__main__":
    main()
