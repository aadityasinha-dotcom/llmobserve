"""GET /v1/traces and /v1/traces/{id}.

The two properties worth defending here are the ones that are invisible when
they break: a page of traces must cost a constant number of statements no matter
how many observations it covers, and paging must not drop or repeat a row when
traces share a timestamp. Both are asserted directly rather than inferred.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text

from app.db import get_engine
from app.services.cursor import encode_cursor
from tests.conftest import Tenant, tenant_session


async def _insert_trace(
    project_id: UUID,
    *,
    trace_id: UUID | None = None,
    name: str = "run",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> UUID:
    trace_id = trace_id or uuid4()
    started_at = started_at or datetime.now(UTC)
    async with tenant_session(project_id) as session:
        await session.execute(
            text(
                "INSERT INTO traces (id, project_id, name, started_at, ended_at) "
                "VALUES (:id, :pid, :name, :started, :ended)"
            ),
            {
                "id": trace_id,
                "pid": project_id,
                "name": name,
                "started": started_at,
                "ended": ended_at,
            },
        )
    return trace_id


async def _insert_observation(
    project_id: UUID,
    trace_id: UUID,
    *,
    started_at: datetime,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: str = "0",
    latency_ms: int = 0,
    parent_id: UUID | None = None,
    payload: str = '{"text": "hi"}',
) -> UUID:
    """Insert an observation and widen the parent trace's stored window.

    The window update mirrors what the ingest upsert does. The read path relies
    on it to bound its scan of the partitioned table, so a fixture that skipped
    it would be testing a state the application never produces.
    """
    observation_id = uuid4()
    async with tenant_session(project_id) as session:
        await session.execute(
            text(
                "INSERT INTO observations (id, project_id, trace_id, parent_observation_id, "
                "type, name, model, input, output, prompt_tokens, completion_tokens, "
                "cost_usd, latency_ms, started_at) "
                "VALUES (:id, :pid, :tid, :parent, 'generation', 'call', 'gpt-4o-mini', "
                # CAST(...) rather than ::jsonb: text() reads a leading colon as a
                # bind parameter, so the postgres cast operator collides with it.
                "CAST(:payload AS jsonb), CAST(:payload AS jsonb), "
                ":pt, :ct, :cost, :lat, :started)"
            ),
            {
                "id": observation_id,
                "pid": project_id,
                "tid": trace_id,
                "parent": parent_id,
                "payload": payload,
                "pt": prompt_tokens,
                "ct": completion_tokens,
                "cost": cost_usd,
                "lat": latency_ms,
                "started": started_at,
            },
        )
        await session.execute(
            text(
                "UPDATE traces SET "
                "  observations_started_min = least(observations_started_min, :started), "
                "  observations_started_max = greatest(observations_started_max, :started) "
                "WHERE id = :tid"
            ),
            {"tid": trace_id, "started": started_at},
        )
    return observation_id


async def test_list_returns_aggregates_and_hides_payloads(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    now = datetime.now(UTC)
    trace_id = await _insert_trace(
        a.project_id, started_at=now, ended_at=now + timedelta(seconds=2)
    )
    for index in range(3):
        await _insert_observation(
            a.project_id,
            trace_id,
            started_at=now + timedelta(seconds=index),
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd="0.00012500",
            latency_ms=100,
        )

    response = await client.get("/v1/traces", headers=a.headers)
    assert response.status_code == 200
    row = next(item for item in response.json()["data"] if item["id"] == str(trace_id))

    assert row["observation_count"] == 3
    assert row["prompt_tokens"] == 30
    assert row["completion_tokens"] == 15
    assert row["total_tokens"] == 45
    assert row["total_latency_ms"] == 300
    # String, not float: summed Numeric(18,8) must not round on the wire.
    assert row["total_cost_usd"] == "0.00037500"
    # Wall clock, distinct from the summed latency above.
    assert row["duration_ms"] == 2000

    # The whole point of the list endpoint: no payloads.
    assert "input" not in row
    assert "output" not in row


async def test_trace_with_no_observations_lists_with_zeros(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """A stub or in-flight trace must appear, not be hidden by an inner join."""
    a, _ = tenants
    trace_id = await _insert_trace(a.project_id, name="empty")

    response = await client.get("/v1/traces", params={"name": "empty"}, headers=a.headers)
    assert response.status_code == 200
    rows = response.json()["data"]
    assert [item["id"] for item in rows] == [str(trace_id)]
    assert rows[0]["observation_count"] == 0
    assert rows[0]["total_cost_usd"] == "0"
    assert rows[0]["duration_ms"] is None


async def test_list_aggregate_does_not_n_plus_one(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """Statement count must not grow with the number of traces or observations.

    This is the regression that a correctness test would never catch: swapping
    the single GROUP BY for a per-trace subquery returns identical JSON and only
    shows up as latency under real data.
    """
    a, _ = tenants
    now = datetime.now(UTC)
    for trace_index in range(8):
        trace_id = await _insert_trace(
            a.project_id, name="n-plus-one", started_at=now - timedelta(seconds=trace_index)
        )
        for obs_index in range(4):
            await _insert_observation(
                a.project_id,
                trace_id,
                started_at=now - timedelta(seconds=trace_index) + timedelta(milliseconds=obs_index),
                prompt_tokens=1,
            )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await client.get(
            "/v1/traces", params={"name": "n-plus-one", "limit": 50}, headers=a.headers
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert len(response.json()["data"]) == 8

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # "FROM observations", not merely "observations": the page query selects the
    # traces.observations_started_min/max columns and would match a looser test.
    reads_observations = [s for s in selects if "from observations" in s.lower()]
    # Exactly one statement touches observations, regardless of 8 traces x 4 rows.
    assert len(reads_observations) == 1, reads_observations
    # And it is bounded on the partition key, or it would fan out across every
    # daily partition ever created.
    assert "started_at >=" in reads_observations[0]
    assert "started_at <=" in reads_observations[0]


async def test_cursor_is_stable_when_traces_share_a_timestamp(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """Page through 10 traces on one identical timestamp, 3 at a time.

    With started_at alone as the sort key this is exactly the case that drops
    and repeats rows. Every id must appear once and only once.
    """
    a, _ = tenants
    shared = datetime.now(UTC).replace(microsecond=0)
    created = {await _insert_trace(a.project_id, name="tie", started_at=shared) for _ in range(10)}

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous bound; the loop breaks on has_more
        params: dict[str, object] = {"name": "tie", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/v1/traces", params=params, headers=a.headers)
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["id"] for item in body["data"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]

    assert len(seen) == len(set(seen)), "a trace was returned on two different pages"
    assert set(seen) == {str(t) for t in created}


async def test_malformed_cursor_is_rejected(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """400, not a silent restart from page one."""
    a, _ = tenants
    response = await client.get("/v1/traces", params={"cursor": "nonsense"}, headers=a.headers)
    assert response.status_code == 400


async def test_filters_by_name_and_started_at_range(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    now = datetime.now(UTC)
    old = await _insert_trace(a.project_id, name="filtered", started_at=now - timedelta(days=2))
    recent = await _insert_trace(a.project_id, name="filtered", started_at=now)
    await _insert_trace(a.project_id, name="other", started_at=now)

    by_name = await client.get("/v1/traces", params={"name": "filtered"}, headers=a.headers)
    assert {item["id"] for item in by_name.json()["data"]} == {str(old), str(recent)}

    windowed = await client.get(
        "/v1/traces",
        params={"name": "filtered", "from": (now - timedelta(hours=1)).isoformat()},
        headers=a.headers,
    )
    assert [item["id"] for item in windowed.json()["data"]] == [str(recent)]


async def test_detail_returns_flat_observations_with_payloads(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    now = datetime.now(UTC)
    trace_id = await _insert_trace(a.project_id, started_at=now)
    parent = await _insert_observation(
        a.project_id, trace_id, started_at=now, prompt_tokens=7, cost_usd="0.00001000"
    )
    child = await _insert_observation(
        a.project_id,
        trace_id,
        started_at=now + timedelta(milliseconds=5),
        parent_id=parent,
        completion_tokens=3,
        cost_usd="0.00002000",
    )

    response = await client.get(f"/v1/traces/{trace_id}", headers=a.headers)
    assert response.status_code == 200
    body = response.json()

    ids = [o["id"] for o in body["observations"]]
    assert ids == [str(parent), str(child)]  # ordered by started_at
    # Flat, with the nesting expressed by parent_observation_id, not by shape.
    assert body["observations"][0]["parent_observation_id"] is None
    assert body["observations"][1]["parent_observation_id"] == str(parent)
    # Payloads present here, unlike the list.
    assert body["observations"][0]["input"] == {"text": "hi"}
    assert body["total_cost_usd"] == "0.00003000"
    assert body["observation_count"] == 2


async def test_detail_of_trace_without_observations_is_200(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    a, _ = tenants
    trace_id = await _insert_trace(a.project_id, name="open")
    response = await client.get(f"/v1/traces/{trace_id}", headers=a.headers)
    assert response.status_code == 200
    assert response.json()["observations"] == []


async def test_detail_keeps_a_dangling_parent_id(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """A parent still in flight must not cause the child to be dropped or nulled."""
    a, _ = tenants
    now = datetime.now(UTC)
    trace_id = await _insert_trace(a.project_id, started_at=now)
    missing_parent = uuid4()
    await _insert_observation(a.project_id, trace_id, started_at=now, parent_id=missing_parent)

    response = await client.get(f"/v1/traces/{trace_id}", headers=a.headers)
    assert response.status_code == 200
    assert response.json()["observations"][0]["parent_observation_id"] == str(missing_parent)


@pytest.mark.parametrize("path", ["/v1/traces", "/v1/traces/{id}"])
async def test_reads_require_authentication(client: AsyncClient, path: str) -> None:
    response = await client.get(path.format(id=uuid4()))
    assert response.status_code == 401


async def test_cursor_from_another_page_does_not_leak_across_tenants(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """A cursor is a position, not a capability. B's cursor must show B nothing of A."""
    a, b = tenants
    now = datetime.now(UTC)
    await _insert_trace(a.project_id, name="secret-a", started_at=now)

    stolen = encode_cursor(now + timedelta(days=1), uuid4())
    response = await client.get("/v1/traces", params={"cursor": stolen}, headers=b.headers)
    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_observations_delivered_in_separate_batches_all_remain_visible(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """The eager-flush case: one observation per request, trace event last.

    This is what `llm_metrics.configure(flush_at=1)` actually does, and it is
    the shape that a single-batch test cannot reach. Each request creates or
    hits the same stub trace, so if the stub insert does not widen the stored
    observation window, the window ends up one instant wide and every span
    outside it becomes invisible to the detail endpoint - while the list
    endpoint, whose window spans the whole page, still counts them. The
    symptom is a trace that reports N observations and returns fewer.
    """
    a, _ = tenants
    trace_id = uuid4()
    base = datetime.now(UTC) - timedelta(minutes=5)
    # Deliberately out of order: delivery order must not shape the window.
    offsets = [timedelta(seconds=4), timedelta(0), timedelta(seconds=2)]

    for index, offset in enumerate(offsets):
        response = await client.post(
            "/v1/ingest",
            headers=a.headers,
            json={
                "events": [
                    {
                        "id": str(uuid4()),
                        "type": "generation",
                        "trace_id": str(trace_id),
                        "name": f"step-{index}",
                        "start_time": (base + offset).isoformat(),
                    }
                ]
            },
        )
        assert response.status_code == 202

    # The trace event arrives last, carrying no observations of its own - it
    # must not narrow the window the stubs established.
    assert (
        await client.post(
            "/v1/ingest",
            headers=a.headers,
            json={
                "events": [
                    {
                        "id": str(trace_id),
                        "type": "trace",
                        "name": "eager-flush",
                        "start_time": base.isoformat(),
                    }
                ]
            },
        )
    ).status_code == 202

    listed = await client.get("/v1/traces", params={"name": "eager-flush"}, headers=a.headers)
    assert listed.status_code == 200
    row = listed.json()["data"][0]

    detail = await client.get(f"/v1/traces/{trace_id}", headers=a.headers)
    assert detail.status_code == 200
    observations = detail.json()["observations"]

    # The invariant that actually matters: the two endpoints agree.
    assert row["observation_count"] == 3
    assert len(observations) == 3, observations
    assert {o["name"] for o in observations} == {"step-0", "step-1", "step-2"}
