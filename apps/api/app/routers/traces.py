"""GET /v1/traces and /v1/traces/{trace_id} - the read path for the dashboard.

Three properties this module exists to hold:

* **No N+1.** A page of traces costs two statements, not one per trace. The page
  is selected first, then a single GROUP BY aggregates every observation
  belonging to it. Adding a trace to the page adds a row to an aggregate, not a
  round trip.
* **Every scan of `observations` is bounded.** That table is RANGE partitioned by
  day on started_at and nothing drops old partitions, so a query without a
  started_at predicate touches every partition that has ever existed and gets
  slower every day. The bounds come from `traces.observations_started_min/max`,
  which ingest maintains, so they are exact rather than guessed.
* **Tenancy is Postgres's job.** There is no project_id predicate anywhere below.
  The row-level security policy supplies one, and because `project_id` leads
  every index the planner uses the policy's own qual as the index prefix. Writing
  it by hand would add nothing and would make the read path look like it is
  enforcing something it is not.

Payload discipline: the list selects no `input`/`output` columns at all. They are
the widest values in the schema and the table does not render them, so they are
not fetched, not serialised, and not sent.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, Table, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TIMESTAMP

from app.deps import RequireRead, TenantSession
from app.models import Observation, Trace
from app.schemas.traces import (
    ObservationDetail,
    TraceDetail,
    TraceListItem,
    TraceListResponse,
)
from app.services.cursor import InvalidCursorError, decode_cursor, encode_cursor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["traces"])

_traces: Table = cast(Table, Trace.__table__)
_observations: Table = cast(Table, Observation.__table__)

DEFAULT_LIMIT = 50
# The dashboard's own proxy clamps to 200; matching it here means a caller that
# bypasses the proxy cannot ask for an unbounded page either.
MAX_LIMIT = 200

# Columns for the list. input/output are deliberately absent.
_LIST_COLUMNS = (
    _traces.c.id,
    _traces.c.name,
    _traces.c.user_id,
    _traces.c.session_id,
    _traces.c["metadata"],
    _traces.c.started_at,
    _traces.c.ended_at,
    _traces.c.created_at,
    _traces.c.sdk_version,
    _traces.c.observations_started_min,
    _traces.c.observations_started_max,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a naive filter bound as UTC rather than letting Postgres guess.

    `?from=2026-09-16T12:00` is what a datetime-local input sends, and it parses
    to a naive datetime. Compared against a timestamptz column, Postgres would
    resolve it using the session TimeZone - so the same URL would select
    different traces depending on the connection, silently. Every timestamp this
    service stores is UTC, so that is what an unqualified one means. A bound that
    carries an offset is respected as sent.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _duration_ms(started_at: datetime, ended_at: datetime | None) -> int | None:
    """Wall-clock elapsed time, or None while the trace is still open.

    Distinct from the sum of observation latency, which double-counts nested and
    concurrent spans. This is the number a "Duration" column should show.
    """
    if ended_at is None:
        return None
    return max(0, int((ended_at - started_at).total_seconds() * 1000))


def _observation_window(
    rows: list[Any],
) -> tuple[datetime, datetime] | None:
    """The exact started_at span covering every observation of these traces.

    None when no row has observations, in which case the aggregate query is
    skipped entirely rather than run against a window that matches nothing.

    This is what bounds the partition scan. It is derived from stored values
    rather than inferred from the traces' own timestamps, because a trace's
    started_at is rewritten on every re-send and a long-running trace outlives
    its own start date - so any interval guessed from those would silently
    undercount tokens and cost.
    """
    lows = [row.observations_started_min for row in rows if row.observations_started_min]
    highs = [row.observations_started_max for row in rows if row.observations_started_max]
    if not lows or not highs:
        return None
    return min(lows), max(highs)


def _aggregate_select(trace_ids: list[UUID], window: tuple[datetime, datetime]) -> Select[Any]:
    """One GROUP BY covering the whole page. This is the anti-N+1.

    coalesce inside sum(), not around it: a NULL token count on one observation
    must contribute zero rather than turn the trace's whole total NULL.
    """
    window_start, window_end = window
    return (
        select(
            _observations.c.trace_id,
            func.count().label("observation_count"),
            func.sum(func.coalesce(_observations.c.prompt_tokens, 0)).label("prompt_tokens"),
            func.sum(func.coalesce(_observations.c.completion_tokens, 0)).label(
                "completion_tokens"
            ),
            func.sum(func.coalesce(_observations.c.total_tokens, 0)).label("total_tokens"),
            func.sum(func.coalesce(_observations.c.cost_usd, 0)).label("total_cost_usd"),
            func.sum(func.coalesce(_observations.c.latency_ms, 0)).label("total_latency_ms"),
        )
        .where(_observations.c.trace_id.in_(trace_ids))
        # The partition predicate. Without these two the aggregate fans out
        # across every daily partition ever created.
        .where(_observations.c.started_at >= window_start)
        .where(_observations.c.started_at <= window_end)
        .group_by(_observations.c.trace_id)
    )


async def _aggregates_for(session: AsyncSession, rows: list[Any]) -> dict[UUID, dict[str, Any]]:
    """Roll up observations for a whole page of traces in one statement."""
    window = _observation_window(rows)
    if window is None:
        return {}

    result = await session.execute(_aggregate_select([row.id for row in rows], window))
    return {row.trace_id: dict(row._mapping) for row in result}


def _item_kwargs(row: Any, aggregate: dict[str, Any] | None) -> dict[str, Any]:
    """Combine a trace row with its rollup.

    A trace with no aggregate is not an error and is not hidden: it is a run
    whose observations have not arrived, or a stub created by an observation
    that outran its trace. It lists with zeros, which is why the join that
    produced `aggregate` is an outer one in spirit - the absence is expected.
    """
    aggregate = aggregate or {}
    return {
        "id": row.id,
        "name": row.name,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "metadata": row._mapping["metadata"],
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "created_at": row.created_at,
        "sdk_version": row.sdk_version,
        "duration_ms": _duration_ms(row.started_at, row.ended_at),
        "observation_count": aggregate.get("observation_count", 0),
        "prompt_tokens": aggregate.get("prompt_tokens", 0),
        "completion_tokens": aggregate.get("completion_tokens", 0),
        "total_tokens": aggregate.get("total_tokens", 0),
        "total_cost_usd": aggregate.get("total_cost_usd") or Decimal(0),
        "total_latency_ms": aggregate.get("total_latency_ms", 0),
    }


@router.get(
    "/traces",
    response_model=TraceListResponse,
    summary="List traces, newest first",
    responses={
        400: {"description": "Malformed cursor"},
        401: {"description": "Missing, unrecognised, or revoked API key"},
        403: {"description": "Key lacks the 'read' scope"},
    },
)
async def list_traces(
    session: TenantSession,
    # 403 unless the key carries "read". An ingest key shipped inside a client
    # application must not be able to read payloads back out.
    _scope: RequireRead,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page.")] = None,
    name: Annotated[str | None, Query(description="Exact trace name match.")] = None,
    user_id: Annotated[
        str | None, Query(description="Exact match on the client's end-user id.")
    ] = None,
    session_id: Annotated[
        str | None, Query(description="Exact match on the client's session id.")
    ] = None,
    since: Annotated[
        datetime | None,
        Query(alias="from", description="Only traces with started_at >= this (inclusive)."),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(alias="to", description="Only traces with started_at < this (exclusive)."),
    ] = None,
) -> TraceListResponse:
    """Return one page of traces, newest first, without their payloads.

    Ordered by `(started_at DESC, id DESC)`. The id is not decoration: traces
    written by one batch routinely share a timestamp, and it is the tiebreaker
    that gives the sort a total order, which is the whole basis for the cursor
    being stable across pages.

    The range filters apply to `started_at` - event time, when the run actually
    happened - not to `created_at`, which is when this server received it. They
    differ whenever a client buffered, retried, or was offline.
    """
    stmt = select(*_LIST_COLUMNS)

    if name is not None:
        stmt = stmt.where(_traces.c.name == name)
    if user_id is not None:
        stmt = stmt.where(_traces.c.user_id == user_id)
    if session_id is not None:
        stmt = stmt.where(_traces.c.session_id == session_id)
    since = _as_utc(since)
    until = _as_utc(until)
    if since is not None:
        stmt = stmt.where(_traces.c.started_at >= since)
    if until is not None:
        # Exclusive, so that consecutive ranges tile without overlapping and a
        # trace on the boundary is counted once.
        stmt = stmt.where(_traces.c.started_at < until)

    if cursor is not None:
        try:
            cursor_started_at, cursor_id = decode_cursor(cursor)
        except InvalidCursorError as exc:
            # 400 rather than silently restarting from the first page, which
            # would look like success while quietly re-serving rows.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        # Row-value comparison, which is correct only because both columns sort
        # the same direction. A mixed-direction sort would need this spelled out
        # as `a < x OR (a = x AND b < y)`.
        # literal() with explicit types rather than bare values: the bind for
        # the timestamp must carry timezone awareness, and the id must bind as
        # uuid rather than text, or Postgres compares the row against the wrong
        # types and the seek quietly stops using the index.
        stmt = stmt.where(
            tuple_(_traces.c.started_at, _traces.c.id)
            < tuple_(
                literal(cursor_started_at, TIMESTAMP(timezone=True)),
                literal(cursor_id, PgUUID(as_uuid=True)),
            )
        )

    # One extra row answers has_more without a second COUNT over the table.
    stmt = stmt.order_by(_traces.c.started_at.desc(), _traces.c.id.desc()).limit(limit + 1)

    rows = list((await session.execute(stmt)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    aggregates = await _aggregates_for(session, rows)
    data = [TraceListItem(**_item_kwargs(row, aggregates.get(row.id))) for row in rows]

    next_cursor = encode_cursor(rows[-1].started_at, rows[-1].id) if rows and has_more else None
    return TraceListResponse(data=data, next_cursor=next_cursor, has_more=has_more)


@router.get(
    "/traces/{trace_id}",
    response_model=TraceDetail,
    summary="One trace with all of its observations",
    responses={
        401: {"description": "Missing, unrecognised, or revoked API key"},
        403: {"description": "Key lacks the 'read' scope"},
        404: {"description": "No such trace in this project"},
    },
)
async def get_trace(trace_id: UUID, session: TenantSession, _scope: RequireRead) -> TraceDetail:
    """Return one trace and every observation under it, payloads included.

    Observations come back flat and ordered by started_at, with
    `parent_observation_id` intact; the client assembles the tree. Nesting is not
    validated here - the column carries no foreign key on purpose, so a parent
    that is still in flight is expected rather than corrupt.

    A trace in another project is a 404, not a 403. Distinguishing the two would
    confirm that an id exists, which is a cross-tenant disclosure however small;
    RLS makes the row invisible and the absence is reported as absence.
    """
    trace_row = (
        await session.execute(select(*_LIST_COLUMNS).where(_traces.c.id == trace_id))
    ).first()
    if trace_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found")

    observations: list[ObservationDetail] = []
    window = _observation_window([trace_row])
    if window is not None:
        window_start, window_end = window
        observation_rows = await session.execute(
            select(
                _observations.c.id,
                _observations.c.trace_id,
                _observations.c.parent_observation_id,
                _observations.c.type,
                _observations.c.name,
                _observations.c.model,
                _observations.c.provider,
                _observations.c.input,
                _observations.c.output,
                _observations.c.prompt_tokens,
                _observations.c.completion_tokens,
                _observations.c.total_tokens,
                _observations.c.cost_usd,
                _observations.c.latency_ms,
                _observations.c.level,
                _observations.c.status_message,
                _observations.c["metadata"],
                _observations.c.started_at,
                _observations.c.ended_at,
            )
            .where(_observations.c.trace_id == trace_id)
            .where(_observations.c.started_at >= window_start)
            .where(_observations.c.started_at <= window_end)
            .order_by(_observations.c.started_at.asc(), _observations.c.id.asc())
        )
        observations = [
            ObservationDetail.model_validate(dict(row._mapping)) for row in observation_rows
        ]

    # Summed here from rows already in hand rather than by a third query.
    kwargs = _item_kwargs(
        trace_row,
        {
            "observation_count": len(observations),
            "prompt_tokens": sum(o.prompt_tokens or 0 for o in observations),
            "completion_tokens": sum(o.completion_tokens or 0 for o in observations),
            "total_tokens": sum(o.total_tokens or 0 for o in observations),
            "total_cost_usd": sum(
                (o.cost_usd or Decimal(0) for o in observations), start=Decimal(0)
            ),
            "total_latency_ms": sum(o.latency_ms or 0 for o in observations),
        }
        if observations
        else None,
    )
    return TraceDetail(**kwargs, observations=observations)
