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
/**
 * A judgement about a trace or one of its observations. `data_type` says
 * whether the judgement is in `value` (numeric; booleans stored as 1/0) or in
 * `value_text` (a label). `value` is a decimal string for the same reason cost
 * is — see CostUsd.
 */
export type ScoreDetail = components["schemas"]["ScoreDetail"];

/** Write-path models, sent by the SDK rather than rendered by the dashboard. */
export type IngestTrace = components["schemas"]["IngestTrace"];
export type IngestObservation = components["schemas"]["IngestObservation"];
export type IngestScore = components["schemas"]["IngestScore"];
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

/** The signed-in user and every project they can select. */
export type Me = components["schemas"]["MeOut"];
export type Project = components["schemas"]["ProjectOut"];
export type SessionOut = components["schemas"]["SessionOut"];
export type ApiKey = components["schemas"]["ApiKeyOut"];
/** Carries the raw key. Returned once, at creation, and never again. */
export type ApiKeyCreated = components["schemas"]["ApiKeyCreated"];
