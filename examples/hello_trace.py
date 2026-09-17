#!/usr/bin/env python
"""Send one trace to a locally running llm-observe API.

Run it with the SDK's interpreter, not this repo's — `llm_metrics` lives in the
separate llmobserve-python repo and is not a dependency here:

    export LLM_METRICS_API_KEY=$(cat ~/.llmobserve_dev_key)
    export LLM_METRICS_HOST=http://127.0.0.1:8000
    export LLM_METRICS_DEBUG=1
    ~/git-repo/llmobserve-python/.venv/bin/python examples/hello_trace.py

Start the API first, in another terminal: `make serve`.
"""

import os
import sys
import time

try:
    import llm_metrics
    from llm_metrics import observe
except ModuleNotFoundError:
    sys.exit(
        "llm_metrics is not importable.\n"
        "Run this with the SDK's interpreter:\n"
        "  ~/git-repo/llmobserve-python/.venv/bin/python examples/hello_trace.py"
    )

if not os.environ.get("LLM_METRICS_API_KEY"):
    sys.exit(
        "LLM_METRICS_API_KEY is not set.\n"
        "  export LLM_METRICS_API_KEY=$(cat ~/.llmobserve_dev_key)\n"
        "or issue a new one with:  make key NAME=my-app"
    )

if not os.environ.get("LLM_METRICS_DEBUG"):
    print("! LLM_METRICS_DEBUG is unset - delivery failures will be silent.\n")

# flush_at=1 sends each event as it completes instead of waiting for a full
# batch. Right for a smoke test, wrong for production: it trades throughput for
# immediate feedback.
llm_metrics.configure(flush_at=1, flush_interval=0.5)


@observe(as_type="generation", name="summarise")
def summarise(text: str) -> str:
    """Stands in for a real provider call.

    Nothing here reports a model or token counts, so this observation stores
    cost_usd = NULL - which is correct. Null means unknown, not free. Cost is
    populated by the integrations, e.g.:

        from llm_metrics.integrations.openai import wrap_openai
        client = wrap_openai(OpenAI())

    which records the model and usage the provider actually returned, and the
    server prices it at write time from app/services/pricing.py.
    """
    time.sleep(0.05)
    return text[:40] + "..."


@observe(as_type="tool", name="fetch-document")
def fetch_document(doc_id: str) -> str:
    time.sleep(0.02)
    return f"contents of {doc_id}, which go on for a while and then stop"


@observe(name="handle-request")
def handle_request(doc_id: str) -> str:
    """The root of the trace. Everything called from here nests under it."""
    document = fetch_document(doc_id)
    return summarise(document)


def main() -> None:
    host = os.environ.get("LLM_METRICS_HOST", "https://cloud.llm-observe.dev")
    print(f"sending to {host}")

    result = handle_request("doc-42")
    print(f"result: {result}")

    # The buffer delivers on a background thread, so a short script can exit
    # before anything is sent. flush() blocks until the queue drains.
    llm_metrics.flush()
    llm_metrics.shutdown()

    print("\nflushed. Expect 3 x '202 Accepted' in the API terminal:")
    print("  handle-request  (span, the trace root)")
    print("    fetch-document  (tool)")
    print("    summarise       (generation)")
    print("\nNo 'dropped N event(s)' above means it landed.")


if __name__ == "__main__":
    main()
