import { NextResponse, type NextRequest } from "next/server";

import { MissingApiConfigError } from "@/lib/api/config";
import { ApiError } from "@/lib/api/errors";
import { apiRequestRaw } from "@/lib/api/fetch";
import { PROJECT_COOKIE } from "@/lib/auth/cookies";
import { SCALAR_FILTERS, TAG_FILTER } from "@/lib/trace-filters";

/**
 * Proxies the trace list to the backend as the signed-in user.
 *
 * The browser calls this route with its httpOnly session cookie; the route
 * forwards that session as a bearer token, so the backend applies exactly the
 * user's own memberships. The dashboard holds no credential of its own - with
 * no session the backend answers 401 and that is what the browser gets.
 *
 * The response is re-emitted rather than forwarded verbatim so no backend
 * header (cookies, internal routing hints) reaches the client.
 */
const BACKEND_TRACES_PATH = "/v1/traces";

// Trace data is never worth serving from a cache.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 200;

/** Only these reach the backend — an unknown param is dropped, not forwarded. */
const PASSTHROUGH_PARAMS = ["cursor", ...SCALAR_FILTERS] as const;

function buildQuery(searchParams: URLSearchParams): Record<string, string | string[]> {
  const query: Record<string, string | string[]> = {};

  for (const key of PASSTHROUGH_PARAMS) {
    const value = searchParams.get(key);
    if (value !== null && value !== "") query[key] = value;
  }
  // Repeatable: the backend requires every tag given, so each is forwarded
  // as its own parameter rather than joined.
  const tags = searchParams.getAll(TAG_FILTER).filter((tag) => tag !== "");
  if (tags.length > 0) query[TAG_FILTER] = tags;

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
    // The selected project, as-is from the cookie. The backend checks the
    // user's membership, so a forged value gets a 404, never another's data.
    const projectId = request.cookies.get(PROJECT_COOKIE)?.value;
    const response = await apiRequestRaw("get", BACKEND_TRACES_PATH, {
      query,
      signal: request.signal,
      headers: projectId ? { "x-project-id": projectId } : undefined,
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
