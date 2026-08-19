"""Model pricing, applied at write time and then frozen.

From CLAUDE.md: "Cost is computed server-side and frozen. Read token counts from
the payload, look up the price at write time, store the resulting cost_usd.
Never recompute historical cost from current prices."

Two consequences follow from "frozen", and they drive the design here:

1. **A wrong price is permanently wrong.** Nothing recomputes it later, so a bad
   entry in this table silently corrupts every row written while it was in
   force. That makes omission strictly safer than a guess: an unknown model
   yields NULL, which is a visible gap someone can backfill, whereas an invented
   price is indistinguishable from a real one.
2. **Editing an entry does not rewrite history.** Rows already written keep the
   cost they were given. When a provider changes a price, update the value here
   and record it in the changelog below; do not attempt to restate old rows.

Prices are USD per 1,000,000 tokens, held as `Decimal` and never as `float` -
binary floating point cannot represent 0.15 exactly, and these values are
multiplied by token counts in the millions.

Deliberately NOT modelled here: prompt-caching read/write multipliers, batch
discounts, and data-residency multipliers. The ingest payload carries a single
prompt/completion token pair with no way to tell cached from uncached tokens, so
applying those rates would be guesswork. Traffic using them is under-reported
rather than mis-reported. Widening the SDK contract to carry cache token counts
is the fix, and it belongs in the SDK repo first.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)

# Bump when any price below changes. Stored nowhere yet - see the note in
# compute_cost_usd about what it would take to attribute a stored cost to the
# exact pricebook that produced it.
PRICEBOOK_VERSION = "2026-08-19"

# cost_usd is NUMERIC(18, 8); quantise to match so the value written is exactly
# the value computed rather than whatever Postgres rounds it to.
_COST_EXPONENT = Decimal("0.00000001")
_PER_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """Base (uncached, non-batch) token prices in USD per million tokens."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


def _p(input_price: str, output_price: str) -> ModelPrice:
    return ModelPrice(Decimal(input_price), Decimal(output_price))


# Keys are normalised model ids (see _normalise). Version-dated ids such as
# `claude-opus-5-20260115` or `gpt-4o-2024-08-06` resolve by longest-prefix
# match, so only the family stem needs an entry - except where a dated snapshot
# is priced differently from its family, which is why gpt-4o-2024-05-13 is
# listed explicitly.
#
# Sources, both retrieved 2026-08-19:
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/pricing
_PRICEBOOK: dict[str, ModelPrice] = {
    # --- Anthropic ---------------------------------------------------------
    "claude-fable-5": _p("10", "50"),
    "claude-mythos-5": _p("10", "50"),
    "claude-opus-5": _p("5", "25"),
    "claude-opus-4-8": _p("5", "25"),
    "claude-opus-4-7": _p("5", "25"),
    "claude-opus-4-6": _p("5", "25"),
    "claude-opus-4-5": _p("5", "25"),
    "claude-opus-4-1": _p("15", "75"),
    "claude-opus-4": _p("15", "75"),
    "claude-sonnet-5": _p("2", "10"),
    "claude-sonnet-4-6": _p("3", "15"),
    "claude-sonnet-4-5": _p("3", "15"),
    "claude-sonnet-4": _p("3", "15"),
    "claude-haiku-4-5": _p("1", "5"),
    "claude-3-5-haiku": _p("0.80", "4"),
    # --- OpenAI ------------------------------------------------------------
    "gpt-5.6-sol": _p("5", "30"),
    "gpt-5.6-terra": _p("2", "12"),
    "gpt-5.6-luna": _p("0.20", "1.20"),
    "gpt-5.5-pro": _p("30", "180"),
    "gpt-5.5": _p("5", "30"),
    "gpt-5.4-pro": _p("30", "180"),
    "gpt-5.4-mini": _p("0.75", "4.50"),
    "gpt-5.4-nano": _p("0.20", "1.25"),
    "gpt-5.4": _p("2.50", "15"),
    "gpt-5.2-pro": _p("21", "168"),
    "gpt-5.2": _p("1.75", "14"),
    "gpt-5.1": _p("1.25", "10"),
    "gpt-5-pro": _p("15", "120"),
    "gpt-5-mini": _p("0.25", "2"),
    "gpt-5-nano": _p("0.05", "0.40"),
    "gpt-5": _p("1.25", "10"),
    "gpt-4.1-mini": _p("0.40", "1.60"),
    "gpt-4.1-nano": _p("0.10", "0.40"),
    "gpt-4.1": _p("2", "8"),
    # The 2024-05-13 snapshot is priced above the family it belongs to; the
    # exact-match pass below is what keeps it from being rounded down to gpt-4o.
    "gpt-4o-2024-05-13": _p("5", "15"),
    "gpt-4o-mini": _p("0.15", "0.60"),
    "gpt-4o": _p("2.50", "10"),
    "o1-pro": _p("150", "600"),
    "o1": _p("15", "60"),
    "o3-pro": _p("20", "80"),
    "o3-mini": _p("1.10", "4.40"),
    "o3": _p("2", "8"),
    "o4-mini": _p("1.10", "4.40"),
}

# Provider-qualified ids arrive from gateways (OpenRouter, LiteLLM, Bedrock) and
# from OpenTelemetry's gen_ai.request.model. Strip the qualifier, keep the model.
_VENDOR_PREFIXES = (
    "openai/",
    "anthropic/",
    "azure/",
    "bedrock/",
    "vertex_ai/",
    "vertexai/",
    "google/",
    "models/",
    "us.",
    "eu.",
    "apac.",
)

# Bounded so an SDK sending unique ids per request cannot grow this without
# limit. Purely for log deduplication; nothing reads it back.
_MAX_TRACKED_UNKNOWN = 512
_unknown_models_seen: set[str] = set()


def _normalise(model: str) -> str:
    """Reduce a provider model id to a pricebook key.

    Lowercases, strips vendor qualifiers, and drops the `-v1:0`-style suffix
    Bedrock appends. Everything else is left intact - the version date is what
    the prefix match consumes.
    """
    name = model.strip().lower()

    changed = True
    while changed:
        changed = False
        for prefix in _VENDOR_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                changed = True

    # Bedrock: anthropic.claude-sonnet-4-5-20250929-v1:0
    if ":" in name:
        name = name.split(":", 1)[0]
    if name.endswith("-v1") or name.endswith("-v2"):
        name = name[:-3]

    # Bedrock also uses a dot between vendor and model.
    for vendor in ("anthropic.", "openai.", "meta.", "mistral."):
        if name.startswith(vendor):
            name = name[len(vendor) :]

    return name


def lookup_price(model: str | None) -> ModelPrice | None:
    """Resolve a model id to its price, or None when it is not in the pricebook.

    Exact match wins, then the longest matching prefix. The prefix must end on a
    `-` boundary so that `gpt-4` cannot claim `gpt-45`, and longest-first is what
    lets `gpt-4o-mini-2024-07-18` resolve to gpt-4o-mini rather than gpt-4o.
    """
    if not model:
        return None

    name = _normalise(model)
    if not name:
        return None

    exact = _PRICEBOOK.get(name)
    if exact is not None:
        return exact

    best_key = ""
    for key in _PRICEBOOK:
        if len(key) > len(best_key) and name.startswith(key + "-"):
            best_key = key

    if best_key:
        return _PRICEBOOK[best_key]

    if name not in _unknown_models_seen:
        if len(_unknown_models_seen) < _MAX_TRACKED_UNKNOWN:
            _unknown_models_seen.add(name)
        # Warn, not error: an unpriced model must not fail ingest. This is the
        # signal that the pricebook needs an entry.
        logger.warning("No price for model %r (normalised: %r); cost_usd will be NULL", model, name)
    return None


def compute_cost_usd(
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> Decimal | None:
    """Cost of one observation in USD, or None when it cannot be established.

    None - rather than 0 - whenever the model is unknown or no token counts were
    reported. Zero would be a claim that the call was free; NULL is a claim that
    the cost is unknown, and only the second is true. Aggregations must therefore
    treat NULL as "unmeasured", not as no spend.

    A missing count on one side is treated as zero once the other side is
    present: a streamed response often reports prompt tokens before completion
    tokens exist, and charging for the half that is known beats discarding both.

    The returned value is what gets stored. If you later need to attribute a cost
    back to the exact prices that produced it, that requires a new column
    (pricebook_version) rather than a lookup - by then this table has moved on.
    """
    price = lookup_price(model)
    if price is None:
        return None

    if prompt_tokens is None and completion_tokens is None:
        return None

    prompt = prompt_tokens or 0
    completion = completion_tokens or 0

    total = (prompt * price.input_usd_per_mtok + completion * price.output_usd_per_mtok) / _PER_MTOK

    return total.quantize(_COST_EXPONENT, rounding=ROUND_HALF_UP)


def known_models() -> list[str]:
    """Pricebook keys, sorted. For diagnostics and tests."""
    return sorted(_PRICEBOOK)
