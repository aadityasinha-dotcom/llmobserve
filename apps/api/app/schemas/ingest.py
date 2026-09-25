"""Request/response models for POST /v1/ingest.

Two rules from CLAUDE.md shape everything here:

* "Accept unknown fields." Older SDKs send older payloads and newer ones send
  keys this server has never heard of. Every model uses `extra="ignore"`, and
  optionality is the default. A payload should only be rejected when it is
  structurally unusable, never because it carried something extra.
* "Cost is computed server-side and frozen." There is deliberately no cost
  field on the way in. If a client sends one it is dropped by `extra="ignore"`.

These are wire contracts, not ORM models. CI publishes them as openapi.json and
the SDK repo validates against that artifact, so changing a field name or
tightening a type here breaks the SDK's CI by design.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

# Batch ceilings. Size limits are not the same thing as schema strictness: a
# payload can be well-formed and still too large to accept in one request.
MAX_TRACES_PER_BATCH = 1000
MAX_OBSERVATIONS_PER_TRACE = 2000
# Ceiling for the flat channel, where observations are not divided among traces.
# Parsing stops here; app.config.ingest_max_observations_per_batch is the
# operator-tunable limit the router applies on top, and answers 413.
MAX_OBSERVATIONS_PER_BATCH = 10_000
MAX_SCORES_PER_BATCH = 10_000

# Attribution ceilings. Trimmed rather than rejected: a client that tags a
# trace with sixty labels has a bug, but losing its trace over it is worse.
MAX_TAGS_PER_TRACE = 32
MAX_TAG_CHARS = 64
MAX_ENVIRONMENT_CHARS = 64
MAX_RELEASE_CHARS = 128
MAX_PROMPT_NAME_CHARS = 255
MAX_PROMPT_VERSION_CHARS = 64
MAX_SCORE_LABEL_CHARS = 255

_BASE_CONFIG = ConfigDict(
    extra="ignore",
    populate_by_name=True,
    str_strip_whitespace=True,
    # `model` is a legitimate field name here; opt out of Pydantic's
    # model_-prefix protection so it does not warn.
    protected_namespaces=(),
)


def _coerce_count(value: Any) -> Any:
    """Round a numeric measurement to a non-negative integer.

    The Python SDK times spans with perf_counter and sends
    `latency_ms: 50.477910990593955`. The column is an integer, and Pydantic
    refuses a float with a fractional part outright - so without this a whole
    batch 422s over sub-millisecond precision nobody asked for. Rounding is the
    obviously intended reading.

    Negatives are clamped rather than rejected for the same reason: a backwards
    clock produces a nonsensical duration, but losing the entire batch over it
    is worse than storing zero.

    Non-numeric input is passed through untouched so that Pydantic still reports
    a normal type error rather than this function inventing a value.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return max(0, round(value))


# The wire accepts any number here; what is stored is a rounded integer. Declared
# explicitly because openapi.json is the SDK's contract - publishing `integer`
# would make the SDK's own CI reject the floats it legitimately sends.
_NumericCount = Annotated[
    int | None,
    BeforeValidator(_coerce_count),
    WithJsonSchema({"type": ["number", "null"], "minimum": 0}),
]


def _clip(value: Any, limit: int) -> Any:
    """Truncate a string to the column width instead of failing the batch."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _coerce_tags(value: Any) -> Any:
    """Reduce whatever arrived to a bounded list of distinct, non-empty strings.

    A bare string is one tag; anything that is not a string or a list of them
    is dropped rather than rejected. Order is preserved, duplicates are not.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()[:MAX_TAG_CHARS]
        if tag and tag not in seen:
            seen.append(tag)
        if len(seen) >= MAX_TAGS_PER_TRACE:
            break
    return seen


def _coerce_version(value: Any) -> Any:
    """Prompt versions arrive as ints from some clients; the column is text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return _clip(value, MAX_PROMPT_VERSION_CHARS)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise to UTC, assuming naive timestamps are already UTC.

    Naive datetimes are common from SDKs that use datetime.utcnow(). Rejecting
    them would 4xx a structurally valid payload, so they are interpreted rather
    than refused.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class IngestObservation(BaseModel):
    """One step inside a trace: an LLM generation, a span, or a discrete event."""

    model_config = _BASE_CONFIG

    # Client-supplied and used as the idempotency key. Generated here only when
    # absent, in which case retries of this observation cannot be deduplicated.
    id: UUID = Field(default_factory=uuid4)

    # Redundant when nested under a trace; honoured when present so the SDK may
    # also send a flat list of observations for a trace sent in an earlier batch.
    trace_id: UUID | None = None

    parent_observation_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_observation_id", "parent_id", "parent"),
    )

    # span | generation | event. Free text on purpose: an unrecognised type from
    # a newer SDK is stored, not rejected.
    type: str = "span"
    name: str | None = None

    model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("model", "gen_ai.request.model", "gen_ai.response.model"),
    )
    provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices("provider", "gen_ai.system"),
    )

    # Prompt and completion bodies. Shape is whatever the provider used, so this
    # stays untyped and lands in JSONB.
    input: Any | None = None
    output: Any | None = None

    # OpenTelemetry GenAI calls these input_tokens / output_tokens; older SDK
    # builds send prompt_tokens / completion_tokens. Accept both spellings.
    prompt_tokens: _NumericCount = Field(
        default=None,
        validation_alias=AliasChoices("prompt_tokens", "input_tokens", "gen_ai.usage.input_tokens"),
    )
    completion_tokens: _NumericCount = Field(
        default=None,
        validation_alias=AliasChoices(
            "completion_tokens", "output_tokens", "gen_ai.usage.output_tokens"
        ),
    )

    # Subsets of the two totals above. Cached tokens are priced at a fraction of
    # fresh ones, which is the whole reason to carry the split. The SDK
    # normalises prompt_tokens to *include* the cached part for every provider.
    cached_tokens: _NumericCount = Field(
        default=None,
        validation_alias=AliasChoices(
            "cached_tokens",
            "cache_read_input_tokens",
            "gen_ai.usage.cache_read.input_tokens",
        ),
    )
    reasoning_tokens: _NumericCount = Field(
        default=None,
        validation_alias=AliasChoices("reasoning_tokens", "gen_ai.usage.reasoning_tokens"),
    )

    latency_ms: _NumericCount = Field(default=None)

    # Which prompt template produced this call. Free text now; the prompts
    # table is a later phase and will key off the same pair.
    prompt_name: Annotated[
        str | None, BeforeValidator(lambda v: _clip(v, MAX_PROMPT_NAME_CHARS))
    ] = Field(default=None)
    prompt_version: Annotated[str | None, BeforeValidator(_coerce_version)] = Field(default=None)

    # The SDK sends `status`, carrying the OpenTelemetry span status codes
    # ("ok" / "error"). There is no separate status column, and dropping it
    # would silently lose the one field that says whether the call failed, so it
    # folds into level. An explicit `level` wins if a client sends both.
    level: str | None = Field(default=None, validation_alias=AliasChoices("level", "status"))
    status_message: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    started_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("started_at", "start_time", "startTime"),
    )
    ended_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("ended_at", "end_time", "endTime"),
    )

    @field_validator("started_at", "ended_at")
    @classmethod
    def _normalise_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @model_validator(mode="after")
    def _derive_latency(self) -> "IngestObservation":
        """Fill in latency from the timestamps when the client did not send it.

        The client's own measurement wins if present: it is closer to the call
        and does not include the SDK's own buffering.
        """
        if self.latency_ms is None and self.started_at and self.ended_at:
            delta = (self.ended_at - self.started_at).total_seconds() * 1000
            if delta >= 0:
                self.latency_ms = int(delta)
        return self


class IngestTrace(BaseModel):
    """One logical run through the client application, with its observations nested."""

    model_config = _BASE_CONFIG

    id: UUID = Field(default_factory=uuid4)
    name: str | None = None

    # The client's own end user, not a user of this platform.
    user_id: str | None = Field(default=None, validation_alias=AliasChoices("user_id", "userId"))
    session_id: str | None = Field(
        default=None, validation_alias=AliasChoices("session_id", "sessionId")
    )

    # Attribution. Bounded and trimmed rather than validated strictly - see
    # the ceilings at the top of the module.
    tags: Annotated[list[str], BeforeValidator(_coerce_tags)] = Field(default_factory=list)
    environment: Annotated[
        str | None, BeforeValidator(lambda v: _clip(v, MAX_ENVIRONMENT_CHARS))
    ] = Field(default=None)
    release: Annotated[str | None, BeforeValidator(lambda v: _clip(v, MAX_RELEASE_CHARS))] = Field(
        default=None
    )

    metadata: dict[str, Any] = Field(default_factory=dict)

    started_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("started_at", "start_time", "startTime", "timestamp"),
    )
    ended_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("ended_at", "end_time", "endTime"),
    )

    observations: Annotated[
        list[IngestObservation], Field(max_length=MAX_OBSERVATIONS_PER_TRACE)
    ] = Field(default_factory=list)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _normalise_timestamps(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @model_validator(mode="after")
    def _backfill(self) -> "IngestTrace":
        """Make the trace self-consistent before it reaches the router.

        started_at is NOT NULL in the database and is the partition key for the
        observations under it, so a missing value has to be resolved somewhere.
        Deriving it from the earliest observation is more faithful than stamping
        server time; server time is only the last resort.
        """
        child_starts = [o.started_at for o in self.observations if o.started_at]

        if self.started_at is None:
            self.started_at = min(child_starts) if child_starts else datetime.now(UTC)

        for obs in self.observations:
            if obs.trace_id is None:
                obs.trace_id = self.id
            if obs.started_at is None:
                obs.started_at = self.started_at

        return self


# Event `type` marking a trace rather than an observation, in the flat envelope.
TRACE_EVENT_TYPE = "trace"
# Event `type` marking a score. Scores are neither traces nor observations: they
# point at one of those and carry a judgement about it.
SCORE_EVENT_TYPE = "score"


class IngestScore(BaseModel):
    """A judgement about a trace or an observation.

    `value` is a number, a boolean, or a short label; the router splits it into
    the typed columns. A score with neither target id is unusable and the
    router drops it - counted, not 4xxed, so one bad score cannot cost a batch.
    """

    model_config = _BASE_CONFIG

    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID | None = None
    observation_id: UUID | None = None

    name: str = Field(min_length=1, max_length=255)
    # bool before int|float: bool is an int subclass and would otherwise be
    # coerced to 1/0 before the router can tell it was a boolean.
    value: bool | int | float | str

    comment: str | None = None
    # human | llm_judge | heuristic. Free text on purpose (rule 5).
    source: str = Field(default="human", max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    scored_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("scored_at", "timestamp", "created_at"),
    )

    @field_validator("scored_at")
    @classmethod
    def _normalise_timestamp(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @field_validator("value")
    @classmethod
    def _bound_label(cls, value: bool | int | float | str) -> bool | int | float | str:
        if isinstance(value, str):
            return value[:MAX_SCORE_LABEL_CHARS]
        return value

    @property
    def has_target(self) -> bool:
        return self.trace_id is not None or self.observation_id is not None


class IngestBatch(BaseModel):
    """The POST /v1/ingest request body. Two envelopes are accepted.

    Flat (what the Python SDK sends), a single stream discriminated by `type`::

        {"events": [{"id": ..., "type": "trace", ...},
                    {"id": ..., "type": "generation", "trace_id": ..., ...},
                    {"id": ..., "type": "score", "trace_id": ..., "name": ..., "value": ...}]}

    Nested, where observations are carried inside their trace::

        {"traces": [{"id": ..., "observations": [...]}]}

    The flat form is the one that suits a buffering client: spans finish
    independently and flush in whatever order they complete, so requiring the
    nesting would force the SDK to hold a trace open until its last child
    returned - and a long-running trace could not be reported until it ended.
    Nothing is lost by accepting it, because every observation carries its own
    trace_id and the router already creates a placeholder for a trace it has not
    seen yet.

    Both normalise to the same internal shape before the router runs: `traces`
    for trace rows, `observations` for the flat remainder.
    """

    model_config = _BASE_CONFIG

    traces: Annotated[list[IngestTrace], Field(max_length=MAX_TRACES_PER_BATCH)] = Field(
        default_factory=list
    )

    # Observations not nested under a trace in this batch. Populated from
    # `events`, and also accepted directly. Their trace may be elsewhere in the
    # batch, may have been sent earlier, or may not have arrived yet.
    observations: Annotated[
        list[IngestObservation], Field(max_length=MAX_OBSERVATIONS_PER_BATCH)
    ] = Field(default_factory=list)

    # Judgements about traces and observations, from `events` or sent directly.
    scores: Annotated[list[IngestScore], Field(max_length=MAX_SCORES_PER_BATCH)] = Field(
        default_factory=list
    )

    # Also arrives as the X-SDK-Version header; the body copy is a fallback for
    # transports that cannot set headers cleanly. The header wins in the router.
    sdk_version: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_envelope(cls, data: Any) -> Any:
        """Accept either envelope, and refuse a body that is neither.

        Runs before field validation so that everything downstream - aliases,
        timestamp coercion, latency derivation - applies identically to both
        forms. An event with no recognised `type` is treated as an observation:
        rule 5 says a newer SDK's unfamiliar span kind must be stored rather
        than rejected, and only `type == "trace"` changes the routing.

        Requiring one of the keys is the single place this schema is strict, and
        it is deliberate. A client whose envelope had drifted would otherwise
        get 202 with a count of zero for every batch - reporting success while
        storing nothing, which is the worst failure available. That is not in
        tension with rule 5: an unrecognised *field* is ignored, an unrecognised
        *body* is a contract mismatch worth surfacing. It is exactly how the
        mismatch with the SDK surfaced.
        """
        if not isinstance(data, dict):
            return data

        recognised = [key for key in ("events", "traces", "observations", "scores") if key in data]
        if not recognised:
            raise ValueError(
                "Request body must contain 'events' (flat) or 'traces' (nested). "
                "An empty list is accepted; the key itself is not optional."
            )

        events = data.get("events")
        if not isinstance(events, list):
            return data

        traces: list[Any] = list(data.get("traces") or [])
        observations: list[Any] = list(data.get("observations") or [])
        scores: list[Any] = list(data.get("scores") or [])
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == TRACE_EVENT_TYPE:
                traces.append(event)
            elif kind == SCORE_EVENT_TYPE:
                scores.append(event)
            else:
                observations.append(event)

        return {**data, "traces": traces, "observations": observations, "scores": scores}

    @model_validator(mode="after")
    def _backfill_flat_observations(self) -> "IngestBatch":
        """Give every flat observation a timestamp.

        started_at is NOT NULL and is the partition key, so it has to be
        resolved here. A flat observation whose trace is in this same batch
        inherits that trace's start; otherwise server time is the last resort.
        """
        if not self.observations:
            return self

        starts = {t.id: t.started_at for t in self.traces}
        for obs in self.observations:
            if obs.started_at is None:
                obs.started_at = (
                    starts.get(obs.trace_id) if obs.trace_id else None
                ) or datetime.now(UTC)
        return self

    @property
    def observation_count(self) -> int:
        return sum(len(t.observations) for t in self.traces) + len(self.observations)


class IngestAccepted(BaseModel):
    """Response body. 202, because durability is all that is promised here.

    Counts are what was accepted for writing, not what was newly inserted -
    duplicates from a retry are accepted and silently deduplicated, and the
    client must not treat a lower number as an error.
    """

    model_config = ConfigDict(extra="forbid")

    accepted_traces: int
    accepted_observations: int
    accepted_scores: int = 0

    # Observations dropped because started_at fell outside the retention window
    # the observations table is partitioned for. Non-zero is not a request
    # failure - the rest of the batch was written - but it does mean data was
    # discarded, so a client seeing this should check its clock rather than
    # retry. Retrying an out-of-window event cannot succeed; it only ages
    # further out of range.
    rejected_observations: int = 0
    # Scores dropped for naming neither a trace nor an observation. Nothing to
    # retry: the client never said what it was scoring.
    rejected_scores: int = 0
