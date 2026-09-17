import { NextResponse, type NextRequest } from "next/server";

import { MissingApiConfigError } from "@/lib/api/config";
import { ApiError } from "@/lib/api/errors";
import { apiRequestRaw } from "@/lib/api/fetch";

/**
 * Proxies the trace list to the backend, attaching the API key server-side.
 *
 * The browser calls this route; only this route knows LLMOBSERVE_API_KEY. The
 * backend's response is re-emitted rather than forwarded verbatim so no backend
 * header (cookies, internal routing hints) reaches the client.
 *
 * ASSUMPTION — the openapi.json in this repo declares no read endpoints at all
 * (only /healthz, /readyz and POST /v1/ingest), so the path and query names
 * below are not yet backed by the contract and this route will 404 until the
 * backend ships trace reads. The response body is deliberately typed `unknown`:
 * inventing a TraceListResponse interface here would be exactly the
 * hand-written API type the project forbids. When the backend publishes the
 * endpoint, run `make types` and type the payload from the generated schema.
 */
const BACKEND_TRACES_PATH = "/v1/traces";

// Trace data is never worth serving from a cache.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 200;

/** Only these reach the backend — an unknown param is dropped, not forwarded. */
const PASSTHROUGH_PARAMS = [
  "cursor",
  "name",
  "user_id",
  "session_id",
  "from",
  "to",
] as const;

function buildQuery(searchParams: URLSearchParams): Record<string, string> {
  const query: Record<string, string> = {};

  for (const key of PASSTHROUGH_PARAMS) {
    const value = searchParams.get(key);
    if (value !== null && value !== "") query[key] = value;
  }

  // Clamped, never absent: a trace table will hit tens of thousands of rows and
  // an unbounded list must not be requestable from the client.
  const requested = Number.parseInt(searchParams.get("limit") ?? "", 10);
  const limit =
    Number.isFinite(requested) && requested > 0
      ? Math.min(requested, MAX_LIMIT)
      : DEFAULT_LIMIT;
  query.limit = String(limit);

  return query;
}

export async function GET(request: NextRequest) {
  const query = buildQuery(request.nextUrl.searchParams);

  try {
    const response = await apiRequestRaw("get", BACKEND_TRACES_PATH, {
      query,
      signal: request.signal,
    });

    const text = await response.text();
    const contentType = response.headers.get("content-type") ?? "";

    if (!contentType.includes("application/json")) {
      // A non-JSON body from the backend is a misconfiguration (a proxy error
      // page, an HTML 404). Do not echo it to the browser.
      return NextResponse.json(
        {
          error: "backend_returned_non_json",
          message: `Backend responded ${response.status} with ${contentType || "no content type"} for ${BACKEND_TRACES_PATH}.`,
        },
        { status: 502 },
      );
    }

    return new NextResponse(text, {
      status: response.status,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    if (error instanceof MissingApiConfigError) {
      // Server misconfiguration, not the caller's fault. The variable name is
      // safe to surface; its value never is.
      console.error(error.message);
      return NextResponse.json(
        { error: "server_misconfigured", message: `${error.variable} is not set.` },
        { status: 500 },
      );
    }

    if (error instanceof ApiError) {
      console.error(error.message);
      return NextResponse.json(
        { error: "backend_unreachable", message: error.message },
        { status: 502 },
      );
    }

    if (error instanceof Error && error.name === "AbortError") {
      // The client hung up mid-flight; nothing to report.
      return new NextResponse(null, { status: 499 });
    }

    throw error;
  }
}
