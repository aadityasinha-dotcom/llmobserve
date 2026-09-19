"""POST /v1/ingest - the write path.

CLAUDE.md calls this the fast path: "POST /v1/ingest writes and returns. No LLM
calls, no scoring, no expensive aggregation inline." Everything in this module is
either a pricebook dictionary lookup or a bulk INSERT. There is no read-back, no
per-row round trip, and no work that scales with anything but batch size.

Three invariants worth stating outright, because they are easy to break later:

* `project_id` is taken from the authenticated API key and never from the
  payload. The ingest schemas have no such field, so a client cannot express a
  cross-tenant write even by accident, and row-level security would reject it
  anyway - the WITH CHECK clause compares against the transaction's GUC.
* Nothing here 4xxes on unrecognised content. Size is the only thing that can be
  rejected, and that is a property of the request rather than of the schema.
* Cost is computed here, at write time, and stored. It is never derived on read.
"""

import logging
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import Table, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import CurrentProjectId, RequireIngest, SdkVersion, TenantSession
from app.models import Observation, Trace
from app.schemas.ingest import (
    IngestAccepted,
    IngestBatch,
    IngestObservation,
    IngestTrace,
)
from app.services.pricing import compute_cost_usd

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["ingest"])

# Cast because the declarative base types __table__ as FromClause, which has no
# .c accessor as far as mypy is concerned. Writing against the Table rather than
# the ORM entity is deliberate: these are bulk INSERTs with explicit column
# names, so there is no unit of work to involve and no identity map to populate.
_traces: Table = cast(Table, Trace.__table__)
_observations: Table = cast(Table, Observation.__table__)

# Rows per INSERT. Postgres caps a statement at 65535 bind parameters; the widest
# row here is an observation at 19 columns, so 500 rows is ~9.5k parameters -
# comfortably clear, while still collapsing a large batch into a handful of
# round trips.
_CHUNK_ROWS = 500

# Spelled numerically because Starlette renamed the constant
# (HTTP_413_REQUEST_ENTITY_TOO_LARGE -> HTTP_413_CONTENT_TOO_LARGE) and the old
# name now emits a DeprecationWarning. The number is the stable part.
_HTTP_413 = 413


def _chunked(rows: Sequence[dict[str, Any]], size: int) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _trace_row(
    trace: IngestTrace,
    project_id: UUID,
    sdk_version: str | None,
    window: tuple[datetime, datetime] | None,
) -> dict[str, Any]:
    """Build the row for one trace.

    `window` is the started_at span of this batch's observations for the trace,
    or None when the batch carried none. It is merged rather than assigned by
    the upsert, so a later batch widens the span instead of replacing it.
    """
    return {
        "id": trace.id,
        "project_id": project_id,
        "name": trace.name,
        "user_id": trace.user_id,
        "session_id": trace.session_id,
        "metadata": trace.metadata,
        "started_at": trace.started_at,
        "ended_at": trace.ended_at,
        "sdk_version": sdk_version,
        "observations_started_min": window[0] if window else None,
        "observations_started_max": window[1] if window else None,
    }


def _observation_rows(
    batch: IngestBatch, project_id: UUID
) -> tuple[list[dict[str, Any]], dict[UUID, tuple[datetime, datetime]], set[UUID], int]:
    """Flatten the batch into observation rows, pricing each one as it goes.

    Returns, alongside the rows:

    * `windows` - the (earliest, latest) started_at seen per trace id, for every
      trace the batch touches. This is what lets a read bound its scan of the
      partitioned `observations` table; see the Trace model. It must cover
      traces the batch carries explicitly as well as orphans, because under the
      flat envelope a trace event and its observation events arrive as siblings,
      so an explicit trace's observations never appear in `trace.observations`.
    * `orphan_ids` - trace ids with no trace row in this batch, which need stubs.
    * the number of observations refused for falling outside the partition window.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    oldest_accepted = now - timedelta(days=settings.ingest_max_event_age_days)
    newest_accepted = now + timedelta(days=settings.ingest_max_event_future_days)

    rows: list[dict[str, Any]] = []
    explicit_trace_ids = {trace.id for trace in batch.traces}
    windows: dict[UUID, tuple[datetime, datetime]] = {}
    orphan_ids: set[UUID] = set()
    rejected = 0

    def _pairs() -> Iterator[tuple[IngestObservation, IngestTrace | None]]:
        """Every observation in the batch, with its enclosing trace if it had one.

        The nested envelope carries observations inside a trace; the flat
        `events` envelope carries them alongside, attributed only by trace_id.
        Both end up here, and from this point the two are indistinguishable.
        """
        for trace in batch.traces:
            for obs in trace.observations:
                yield obs, trace
        for obs in batch.observations:
            yield obs, None

    for obs, parent in _pairs():
        # Nested observations inherit from their trace; flat ones carry
        # their own values, backfilled by IngestBatch. The fallbacks keep
        # this total rather than trusting a validator from a distance.
        trace_id = obs.trace_id or (parent.id if parent else None)
        started_at = obs.started_at or (parent.started_at if parent else None)
        if started_at is None:  # pragma: no cover - backfill precludes this
            continue
        if trace_id is None:
            # Unattributable: a flat observation with no trace_id at all.
            # Deriving one from the observation's own id keeps the event
            # rather than dropping it, and stays stable across retries -
            # minting a fresh uuid here would create a new stub trace on
            # every redelivery of the same batch.
            trace_id = obs.id

        # Refuse rather than strand. A row written outside the partitioned
        # range lands in observations_default and poisons that day for good:
        # ensure_observations_partitions() will then skip creating the day's
        # partition, so every subsequent write for that day also goes to
        # DEFAULT and queries stop pruning. Dropping one bad event is
        # recoverable; degrading a whole day of storage is not.
        #
        # Per observation, not per batch: one client with a skewed clock
        # must not cost the other 999 events in the request.
        if not (oldest_accepted <= started_at <= newest_accepted):
            rejected += 1
            continue

        seen = windows.get(trace_id)
        windows[trace_id] = (
            (started_at, started_at)
            if seen is None
            else (min(seen[0], started_at), max(seen[1], started_at))
        )
        if trace_id not in explicit_trace_ids:
            orphan_ids.add(trace_id)

        rows.append(
            {
                "id": obs.id,
                "project_id": project_id,
                "trace_id": trace_id,
                "parent_observation_id": obs.parent_observation_id,
                "type": obs.type,
                "name": obs.name,
                "model": obs.model,
                "provider": obs.provider,
                "input": obs.input,
                "output": obs.output,
                "prompt_tokens": obs.prompt_tokens,
                "completion_tokens": obs.completion_tokens,
                # Frozen here. Never recomputed on read.
                "cost_usd": compute_cost_usd(obs.model, obs.prompt_tokens, obs.completion_tokens),
                "latency_ms": obs.latency_ms,
                "level": obs.level,
                "status_message": obs.status_message,
                "metadata": obs.metadata,
                "started_at": started_at,
                "ended_at": obs.ended_at,
            }
        )
        # total_tokens is a generated column and created_at has a server
        # default; writing either would be rejected or would fight the
        # database for authorship.

    return rows, windows, orphan_ids, rejected


async def _upsert_traces(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    """Insert traces, merging rather than discarding when the row already exists.

    CLAUDE.md specifies ON CONFLICT DO NOTHING for idempotency, and observations
    below use exactly that. Traces are the one table where DO NOTHING would lose
    data instead of preserving it, for two reasons:

    * A trace is an envelope that fills in over its lifetime. The SDK sends it
      when the run starts and again when it ends; under DO NOTHING the second
      write is dropped and `ended_at` is never recorded.
    * Observations can arrive before their trace, which creates the stub rows
      below. Under DO NOTHING the real trace would then be discarded in favour
      of the stub, permanently losing name, user_id and session_id.

    The merge is still idempotent, which is what the rule actually requires:
    COALESCE never regresses a value to NULL, `||` on jsonb is stable for
    repeated input, and replaying any batch converges on the same row.
    """
    if not rows:
        return

    for chunk in _chunked(rows, _CHUNK_ROWS):
        stmt = pg_insert(_traces).values(list(chunk))
        excluded = stmt.excluded
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["project_id", "id"],
                set_={
                    "name": func.coalesce(excluded.name, _traces.c.name),
                    "user_id": func.coalesce(excluded.user_id, _traces.c.user_id),
                    "session_id": func.coalesce(excluded.session_id, _traces.c.session_id),
                    # Key-wise merge, so a later partial batch enriches the
                    # metadata already stored instead of replacing it.
                    "metadata": _traces.c["metadata"].op("||")(excluded["metadata"]),
                    # Unconditional: the client's own trace timestamp supersedes
                    # a stub's guess, and a replay writes the same value.
                    "started_at": excluded.started_at,
                    "ended_at": func.coalesce(excluded.ended_at, _traces.c.ended_at),
                    "sdk_version": func.coalesce(excluded.sdk_version, _traces.c.sdk_version),
                    # least/greatest ignore NULLs in Postgres, so a batch that
                    # carries no observations for this trace leaves the stored
                    # span untouched, and one that does only ever widens it.
                    # That is what keeps the span correct when a trace's
                    # observations arrive across many batches, and idempotent
                    # when any of them is replayed.
                    "observations_started_min": func.least(
                        _traces.c.observations_started_min, excluded.observations_started_min
                    ),
                    "observations_started_max": func.greatest(
                        _traces.c.observations_started_max, excluded.observations_started_max
                    ),
                    "updated_at": func.now(),
                },
            )
        )


async def _insert_stub_traces(
    session: AsyncSession,
    project_id: UUID,
    orphan_windows: dict[UUID, tuple[datetime, datetime]],
) -> None:
    """Create placeholder traces for observations whose trace has not arrived.

    `observations.trace_id` carries no foreign key precisely so that this case
    cannot reject the observation (see the model docstring). The stub keeps the
    trace list view from hiding a run whose parent is still in flight.

    DO NOTHING is the correct half of the asymmetry for the *descriptive*
    columns: a stub must never overwrite a real trace, while a real trace must
    overwrite a stub.

    The observation window is the exception, and it has to be, because a client
    that flushes eagerly sends each observation in its own request. Under
    DO NOTHING the first one would create the stub with a window of
    [t, t] and every later observation for that trace would be dropped on the
    floor - leaving a window one instant wide while the trace's own spans fall
    outside it, invisible to every read that bounds its scan by that window.

    So the conflict clause widens the window and touches nothing else. That is
    not a stub guessing: min/max here are measured from observations actually in
    hand, so they are facts a real trace row should absorb too. least/greatest
    ignore NULLs and are order-independent, which keeps this idempotent under
    replay and under out-of-order delivery alike.
    """
    if not orphan_windows:
        return

    rows = [
        {
            "id": trace_id,
            "project_id": project_id,
            # The stub's own timestamp is a guess, corrected when the real trace
            # lands. The observation span is not a guess - it is measured from
            # the observations in hand - so it is recorded even here.
            "started_at": window[0],
            "observations_started_min": window[0],
            "observations_started_max": window[1],
        }
        for trace_id, window in orphan_windows.items()
    ]
    for chunk in _chunked(rows, _CHUNK_ROWS):
        stmt = pg_insert(_traces).values(list(chunk))
        excluded = stmt.excluded
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["project_id", "id"],
                set_={
                    # Only these. Every descriptive column is left exactly as the
                    # existing row has it, so a stub still cannot clobber a real
                    # trace's name, user_id or timestamps.
                    "observations_started_min": func.least(
                        _traces.c.observations_started_min,
                        excluded.observations_started_min,
                    ),
                    "observations_started_max": func.greatest(
                        _traces.c.observations_started_max,
                        excluded.observations_started_max,
                    ),
                    "updated_at": func.now(),
                },
            )
        )


async def _insert_observations(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> None:
    """Insert observations, dropping exact repeats.

    DO NOTHING is right here: an observation records a call that already
    happened, so there is nothing to merge on a retry.

    The conflict target is (project_id, id, started_at) rather than id alone
    because `observations` is partitioned by started_at and Postgres requires the
    partition key in every unique constraint. A client that regenerates its start
    timestamp between retries therefore produces a duplicate. That obligation
    belongs to the SDK: capture start_time once, reuse it across retries.
    """
    if not rows:
        return

    for chunk in _chunked(rows, _CHUNK_ROWS):
        stmt = pg_insert(_observations).values(list(chunk))
        await session.execute(
            stmt.on_conflict_do_nothing(index_elements=["project_id", "id", "started_at"])
        )


@router.post(
    "/ingest",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of traces and observations",
    responses={
        401: {"description": "Missing, unrecognised, or revoked API key"},
        403: {"description": "Key lacks the 'ingest' scope"},
        413: {"description": "Batch exceeds the configured size limits"},
    },
)
async def ingest(
    batch: IngestBatch,
    project_id: CurrentProjectId,
    session: TenantSession,
    sdk_version: SdkVersion,
    # Declared for its side effect: 403 unless the key carries "ingest". A
    # read-only dashboard key must not be able to write traces.
    _scope: RequireIngest,
) -> IngestAccepted:
    """Accept a batch, write it, and return.

    202 rather than 201: what is promised is durability, not that any particular
    row was newly created. A retry of an already-stored batch is a success and
    reports the same counts as the original.
    """
    settings = get_settings()

    trace_count = len(batch.traces)
    observation_count = batch.observation_count

    # Size is the only ground for rejection. It is a property of the request, not
    # of the payload's shape, so it does not violate "accept unknown fields".
    #
    # There are deliberately two tiers. The schema's max_length is a hard ceiling
    # that Pydantic enforces during parsing, before an oversized list is fully
    # materialised - that one is memory safety and it answers 422. The settings
    # below are the operator-tunable limit and answer 413, which is the more
    # accurate status. So lowering a setting below its schema constant is what
    # makes these branches fire for traces; for observations the settings cap is
    # a batch total while the schema cap is per trace, so it is reachable either
    # way.
    if trace_count > settings.ingest_max_traces_per_batch:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"Batch carries {trace_count} traces; the limit is "
                f"{settings.ingest_max_traces_per_batch}. Split it."
            ),
        )
    if observation_count > settings.ingest_max_observations_per_batch:
        raise HTTPException(
            status_code=_HTTP_413,
            detail=(
                f"Batch carries {observation_count} observations; the limit is "
                f"{settings.ingest_max_observations_per_batch}. Split it."
            ),
        )

    # The header is authoritative; the body field is a fallback for transports
    # that cannot set headers cleanly.
    effective_sdk_version = sdk_version or batch.sdk_version

    observation_rows, windows, orphan_ids, rejected = _observation_rows(batch, project_id)
    trace_rows = [
        _trace_row(trace, project_id, effective_sdk_version, windows.get(trace.id))
        for trace in batch.traces
    ]
    orphan_windows = {trace_id: windows[trace_id] for trace_id in orphan_ids}

    # Traces first so that the common case - a trace and its observations in one
    # batch - never leaves an observation pointing at a row that does not exist
    # yet. Stubs last of the two, so a real trace in this same batch wins.
    await _upsert_traces(session, trace_rows)
    await _insert_stub_traces(session, project_id, orphan_windows)
    await _insert_observations(session, observation_rows)

    logger.info(
        "ingest accepted project=%s traces=%d observations=%d stubs=%d rejected=%d sdk=%s",
        project_id,
        trace_count,
        len(observation_rows),
        len(orphan_windows),
        rejected,
        effective_sdk_version or "unknown",
    )
    if rejected:
        # Warn separately: dropped data is the one outcome here that a healthy
        # client should never produce, so it deserves its own log line rather
        # than a field buried in an info message.
        logger.warning(
            "ingest dropped %d observation(s) for project=%s: started_at outside the "
            "accepted window (-%dd..+%dd). Check the client clock; retrying will not help.",
            rejected,
            project_id,
            settings.ingest_max_event_age_days,
            settings.ingest_max_event_future_days,
        )

    return IngestAccepted(
        accepted_traces=trace_count,
        accepted_observations=len(observation_rows),
        rejected_observations=rejected,
    )
