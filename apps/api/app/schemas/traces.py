"""Response models for the trace read endpoints.

Two rules shape what is and is not in here.

* **The list carries no payloads.** `input` and `output` are the largest columns
  in the schema and the trace table does not render them, so `TraceListItem` has
  no field for them at all. They appear only on the detail endpoint, where the
  client asked for exactly one trace.
* **Cost is a string on the wire.** `cost_usd` is `Numeric(18, 8)` and is summed
  across every observation of a trace. A JSON float would round those sums, and
  CLAUDE.md freezes cost at write time precisely so nothing downstream reinvents
  it. Serialising as a decimal string is the only representation that survives
  the trip intact; the dashboard formats it for display.

These are wire contracts. CI publishes them as openapi.json and the dashboard
generates its TypeScript from that artifact, so renaming a field here breaks the
frontend's typecheck by design.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PlainSerializer, WithJsonSchema

_BASE_CONFIG = ConfigDict(
    from_attributes=True,
    # `model` is a legitimate field name on an observation.
    protected_namespaces=(),
)


def _decimal_to_str(value: Decimal | None) -> str | None:
    """Render a Decimal in plain notation.

    `format(v, "f")` rather than `str(v)`: str() on a Decimal that came back
    from Postgres as 1E-8 keeps the exponent, and "1E-8" is not a number the
    dashboard's parser should have to handle.
    """
    return None if value is None else format(value, "f")


# Declared as a string in the published schema so the generated TypeScript type
# is `string`. That is deliberate friction - it stops the dashboard from doing
# float arithmetic on money without noticing.
CostUsd = Annotated[
    Decimal,
    PlainSerializer(_decimal_to_str, return_type=str),
    WithJsonSchema({"type": "string", "format": "decimal", "examples": ["0.00123400"]}),
]

OptionalCostUsd = Annotated[
    Decimal | None,
    PlainSerializer(_decimal_to_str, return_type=str | None),
    WithJsonSchema({"type": ["string", "null"], "format": "decimal"}),
]


class TraceAggregates(BaseModel):
    """Per-trace rollups computed from its observations.

    Every field is zero rather than null when a trace has no observations yet.
    That is a real and common state - a trace whose spans are still in flight,
    or a stub created by an observation that arrived before its trace - and a
    table column reading "0" is honest where "-" would suggest an error.
    """

    model_config = _BASE_CONFIG

    observation_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: CostUsd = Decimal(0)

    # The SUM of per-observation latency, which is model time spent, NOT how
    # long the trace took: nested and concurrent spans each contribute, so five
    # parallel 200ms calls total 1000ms inside a trace that lasted 200ms. Use
    # duration_ms for elapsed time.
    total_latency_ms: int = 0


class TraceListItem(TraceAggregates):
    """One row of GET /v1/traces."""

    id: UUID
    name: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    # Required, not defaulted: the column is NOT NULL and every response carries
    # it. A default here would publish the field as optional and make the
    # dashboard null-check something that is never absent.
    metadata: dict[str, Any]

    started_at: datetime
    ended_at: datetime | None = None
    # Server receive time, distinct from started_at: clients buffer and retry,
    # so the gap between the two is real.
    created_at: datetime
    sdk_version: str | None = None

    # Wall-clock elapsed time. None while the trace is still open, which is why
    # it is not merged with total_latency_ms.
    duration_ms: int | None = None


class TraceListResponse(BaseModel):
    """A page of traces plus the cursor that continues it.

    `next_cursor` is opaque and must be echoed back verbatim. It encodes the
    (started_at, id) of the last row on this page, which is what makes paging
    stable when traces share a timestamp - id breaks the tie and the seek uses
    the same pair the ORDER BY does.
    """

    model_config = ConfigDict(from_attributes=True)

    data: list[TraceListItem]
    next_cursor: str | None = None
    has_more: bool = False


class ObservationDetail(BaseModel):
    """One observation, with its payloads.

    Returned flat, with `parent_observation_id` intact, ordered by started_at so
    the client can assemble the tree in a single pass.

    A dangling `parent_observation_id` is normal, not corrupt: the column carries
    no foreign key on purpose, because batches split and retry independently and
    a parent may still be in flight. Render an orphan at the root; do not drop it.
    """

    model_config = _BASE_CONFIG

    id: UUID
    trace_id: UUID
    parent_observation_id: UUID | None = None

    type: str
    name: str | None = None
    model: str | None = None
    provider: str | None = None

    # Shape is whatever the provider used; this is JSONB and stays untyped.
    input: Any | None = None
    output: Any | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: OptionalCostUsd = None
    latency_ms: int | None = None

    level: str | None = None
    status_message: str | None = None
    metadata: dict[str, Any]

    started_at: datetime
    ended_at: datetime | None = None


class TraceDetail(TraceListItem):
    """GET /v1/traces/{trace_id}: one trace and every observation under it.

    Empty `observations` is a 200, never a 404. A trace with no observations is
    a legitimate thing to look at - it may be open, or a stub whose spans have
    not landed - and the trace row itself is what the id addresses.
    """

    observations: list[ObservationDetail]
