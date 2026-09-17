import type { components } from "./schema";

/**
 * Convenience aliases into the generated schema.
 *
 * These are aliases, not definitions — every one resolves into `./schema`, so a
 * backend change surfaces as a type error here rather than as silent drift.
 * Never widen one by hand; regenerate with `make types` instead.
 */

/** One row of the trace table. Carries aggregates and deliberately no payloads. */
export type TraceListItem = components["schemas"]["TraceListItem"];
export type TraceListResponse = components["schemas"]["TraceListResponse"];

/** One trace with every observation under it, payloads included. */
export type TraceDetail = components["schemas"]["TraceDetail"];
export type ObservationDetail = components["schemas"]["ObservationDetail"];

/** Write-path models, sent by the SDK rather than rendered by the dashboard. */
export type IngestTrace = components["schemas"]["IngestTrace"];
export type IngestObservation = components["schemas"]["IngestObservation"];
export type IngestBatch = components["schemas"]["IngestBatch"];
export type IngestAccepted = components["schemas"]["IngestAccepted"];

export type TraceId = TraceListItem["id"];
export type ObservationId = ObservationDetail["id"];

/**
 * Money is a string on the wire, not a number.
 *
 * `cost_usd` is Numeric(18,8) in Postgres and is summed across every
 * observation of a trace. Parsing it into a JS number would round those sums at
 * 2^53 and reintroduce exactly the drift the backend freezes cost at write time
 * to avoid. Format it for display; do not do arithmetic on it here.
 */
export type CostUsd = TraceListItem["total_cost_usd"];
