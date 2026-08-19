"""Pricebook resolution and cost arithmetic.

No database. These are the only pure-unit tests in the suite, and they matter
disproportionately because cost is frozen at write time: a resolution bug does
not produce a wrong number that someone later notices and recomputes, it
produces a wrong number that is now the permanent record.
"""

from decimal import Decimal

import pytest

from app.services.pricing import (
    PRICEBOOK_VERSION,
    compute_cost_usd,
    known_models,
    lookup_price,
)

# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_exact_match() -> None:
    price = lookup_price("gpt-4o")
    assert price is not None
    assert price.input_usd_per_mtok == Decimal("2.50")
    assert price.output_usd_per_mtok == Decimal("10")


@pytest.mark.parametrize(
    ("model", "expected_input"),
    [
        ("claude-opus-5-20260115", Decimal("5")),
        ("claude-sonnet-5-20260304", Decimal("2")),
        ("claude-haiku-4-5-20251001", Decimal("1")),
        ("gpt-4o-2024-08-06", Decimal("2.50")),
    ],
)
def test_dated_snapshots_resolve_to_their_family(model: str, expected_input: Decimal) -> None:
    """SDKs send pinned, dated ids. Only the family stem is in the pricebook."""
    price = lookup_price(model)
    assert price is not None
    assert price.input_usd_per_mtok == expected_input


def test_longest_prefix_wins() -> None:
    """gpt-4o-mini must not be swallowed by gpt-4o, which is also a prefix."""
    price = lookup_price("gpt-4o-mini-2024-07-18")
    assert price is not None
    assert price.input_usd_per_mtok == Decimal("0.15")


def test_exact_match_beats_prefix() -> None:
    """A snapshot priced differently from its family keeps its own price.

    gpt-4o-2024-05-13 is $5/$15 while the gpt-4o family is $2.50/$10. Resolving
    it by prefix would under-bill it by half, permanently.
    """
    price = lookup_price("gpt-4o-2024-05-13")
    assert price is not None
    assert price.input_usd_per_mtok == Decimal("5")
    assert price.output_usd_per_mtok == Decimal("15")


@pytest.mark.parametrize("model", ["gpt-4onda", "gpt-5x", "o1x", "claude-opus-52"])
def test_prefix_match_requires_a_boundary(model: str) -> None:
    """A key may only claim a name it is a dash-separated prefix of.

    Without the boundary check `gpt-4` would claim `gpt-45` and `o1` would claim
    `o1x` - silently pricing an unknown model as a known one, which is the one
    failure mode worse than returning None.
    """
    assert lookup_price(model) is None


@pytest.mark.parametrize(
    ("model", "expected_input"),
    [
        ("anthropic/claude-sonnet-5", Decimal("2")),
        ("us.anthropic.claude-sonnet-5-20260304-v1:0", Decimal("2")),
        ("bedrock/anthropic.claude-sonnet-5", Decimal("2")),
        ("  Claude-Sonnet-5  ", Decimal("2")),
        ("openai/gpt-4o", Decimal("2.50")),
        ("azure/gpt-4o-mini", Decimal("0.15")),
    ],
)
def test_vendor_qualifiers_are_stripped(model: str, expected_input: Decimal) -> None:
    """Gateways and OTel emit provider-qualified ids; the price is the same."""
    price = lookup_price(model)
    assert price is not None
    assert price.input_usd_per_mtok == expected_input


@pytest.mark.parametrize("model", [None, "", "   ", "totally-made-up-model", "llama-9"])
def test_unknown_models_resolve_to_none(model: str | None) -> None:
    """Omission over invention: NULL is a visible gap, a guess is indistinguishable."""
    assert lookup_price(model) is None


# --------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------


def test_cost_is_exact() -> None:
    # 1000 in @ $5/Mtok  = 0.005
    #  500 out @ $25/Mtok = 0.0125
    assert compute_cost_usd("claude-opus-5", 1000, 500) == Decimal("0.01750000")


def test_cost_is_decimal_not_float() -> None:
    cost = compute_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000)
    assert isinstance(cost, Decimal)
    # 0.15 + 0.60. A float would land on 0.7500000000000001.
    assert cost == Decimal("0.75000000")


def test_cost_is_quantised_to_the_column_scale() -> None:
    """cost_usd is NUMERIC(18, 8); the value stored must be the value computed."""
    cost = compute_cost_usd("gpt-5-nano", 1, 0)
    assert cost is not None
    assert cost.as_tuple().exponent == -8


def test_unknown_model_yields_no_cost() -> None:
    assert compute_cost_usd("nope", 100, 100) is None


def test_no_token_counts_yields_no_cost() -> None:
    """None, not zero. Zero asserts the call was free; None admits ignorance."""
    assert compute_cost_usd("gpt-4o", None, None) is None


@pytest.mark.parametrize(
    ("prompt", "completion", "expected"),
    [
        (1_000_000, None, Decimal("2.50000000")),
        (None, 1_000_000, Decimal("10.00000000")),
        (0, 0, Decimal("0.00000000")),
    ],
)
def test_half_known_usage_is_still_charged(
    prompt: int | None, completion: int | None, expected: Decimal
) -> None:
    """A streamed response reports prompt tokens before completion tokens exist."""
    assert compute_cost_usd("gpt-4o", prompt, completion) == expected


# --------------------------------------------------------------------------
# Pricebook hygiene
# --------------------------------------------------------------------------


def test_every_price_is_a_positive_decimal() -> None:
    """A float or a zero here would corrupt every row written while it stood."""
    for model in known_models():
        price = lookup_price(model)
        assert price is not None, model
        for value in (price.input_usd_per_mtok, price.output_usd_per_mtok):
            assert isinstance(value, Decimal), f"{model}: {value!r} is not Decimal"
            assert value > 0, f"{model}: {value} is not positive"


def test_pricebook_keys_are_already_normalised() -> None:
    """Keys are matched against normalised input, so an unnormalised key is dead.

    It would never be reachable and its model would silently price as NULL.
    """
    for model in known_models():
        assert model == model.strip().lower()
        assert lookup_price(model) is not None, f"{model} is unreachable"


def test_pricebook_version_is_a_date() -> None:
    year, month, day = PRICEBOOK_VERSION.split("-")
    assert len(year) == 4 and len(month) == 2 and len(day) == 2
    assert year.isdigit() and month.isdigit() and day.isdigit()
