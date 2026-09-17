import "server-only";

import { getApiConfig } from "./config";
import { ApiError } from "./errors";
import type { paths } from "./schema";

/**
 * Typed fetch wrapper over the backend contract.
 *
 * Every shape here is derived from `./schema` (generated from the backend's
 * openapi.json). Nothing in this file describes the API by hand — if a request
 * or response type looks wrong, regenerate with `make types` rather than
 * patching it here.
 *
 * Server-only. It reads the API key, so importing it from a client component
 * is a build error.
 */

type Method = "get" | "post" | "put" | "patch" | "delete";

/** The paths in the contract that actually declare `M`. */
type PathsWith<M extends Method> = {
  [P in keyof paths]: paths[P] extends { [K in M]: object } ? P : never;
}[keyof paths];

type Operation<P extends keyof paths, M extends Method> = paths[P] extends {
  [K in M]: infer O;
}
  ? O
  : never;

type JsonOf<T> = T extends { content: { "application/json": infer J } }
  ? J
  : never;

/** The 2xx response body declared for an operation. */
type SuccessBody<O> = O extends { responses: infer R }
  ? JsonOf<R[Extract<keyof R, 200 | 201 | 202 | 204>]>
  : never;

type RequestBodyOf<O> = O extends {
  requestBody: { content: { "application/json": infer B } };
}
  ? B
  : undefined;

type QueryOf<O> = O extends { parameters: { query?: infer Q } } ? Q : never;
type PathOf<O> = O extends { parameters: { path: infer P } } ? P : never;

interface CommonOptions {
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Defaults to "no-store": observability data is never worth serving stale. */
  cache?: RequestCache;
  next?: { revalidate?: number | false; tags?: string[] };
}

type QueryOption<O> = [QueryOf<O>] extends [never]
  ? { query?: undefined }
  : { query?: QueryOf<O> };

/** Required when the operation templates its path, absent when it does not. */
type PathOption<O> = [PathOf<O>] extends [never]
  ? { path?: undefined }
  : { path: PathOf<O> };

type GetOptions<O> = CommonOptions & QueryOption<O> & PathOption<O>;
type BodyOptions<O> = CommonOptions &
  QueryOption<O> &
  PathOption<O> & { body: RequestBodyOf<O> };

/** Drops undefined and null; repeats a key per element for array values. */
function toSearchParams(query: unknown): string {
  if (!query || typeof query !== "object") return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query as Record<string, unknown>)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== undefined && item !== null) params.append(key, String(item));
      }
    } else {
      params.append(key, String(value));
    }
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

/**
 * Substitute `{name}` placeholders in a templated path.
 *
 * Every value is percent-encoded. These ids come from a URL the reader can
 * edit, so treating them as trusted path text is how a crafted id escapes the
 * path and addresses a different endpoint. Throws on a missing value rather
 * than requesting a URL with a literal brace in it, which the backend would
 * answer with a confusing 404 or 422.
 */
function applyPathParams(path: string, params: unknown): string {
  if (!params || typeof params !== "object") return path;
  const values = params as Record<string, unknown>;
  return path.replace(/\{([^}]+)\}/g, (_match, key: string) => {
    const value = values[key];
    if (value === undefined || value === null) {
      throw new TypeError(`Missing path parameter "${key}" for ${path}`);
    }
    return encodeURIComponent(String(value));
  });
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined;
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    const text = await response.text();
    return text.length > 0 ? text : undefined;
  }
  try {
    return await response.json();
  } catch {
    // A 200 whose body is not the JSON it claimed to be is still a failure,
    // but the caller learns more from the status than from a parse error.
    return undefined;
  }
}

/**
 * Issues the request and returns the raw `Response`, with auth attached.
 *
 * Use this when the response should be streamed or forwarded rather than
 * parsed — the route handlers proxy with it. Prefer `apiGet`/`apiPost` in
 * application code so the contract types apply.
 */
export async function apiRequestRaw(
  method: string,
  path: string,
  init: {
    query?: unknown;
    path?: unknown;
    body?: unknown;
    headers?: Record<string, string>;
    signal?: AbortSignal;
    cache?: RequestCache;
    next?: { revalidate?: number | false; tags?: string[] };
  } = {},
): Promise<Response> {
  const { baseUrl, apiKey } = getApiConfig();
  const resolved = applyPathParams(path, init.path);
  const url = `${baseUrl}${resolved}${toSearchParams(init.query)}`;

  const headers: Record<string, string> = {
    accept: "application/json",
    ...init.headers,
    // Last, so a caller can never accidentally overwrite credentials.
    authorization: `Bearer ${apiKey}`,
  };
  if (init.body !== undefined) headers["content-type"] = "application/json";

  try {
    return await fetch(url, {
      method: method.toUpperCase(),
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
      cache: init.next ? undefined : (init.cache ?? "no-store"),
      next: init.next,
    });
  } catch (cause) {
    // `path`, not `url` — the base URL can carry an internal hostname.
    throw ApiError.transport(path, cause);
  }
}

async function request(
  method: string,
  path: string,
  options: CommonOptions & { query?: unknown; path?: unknown; body?: unknown },
): Promise<unknown> {
  const response = await apiRequestRaw(method, path, options);
  const body = await readBody(response);

  if (!response.ok) {
    throw new ApiError(
      `${method.toUpperCase()} ${path} failed: ${response.status} ${response.statusText}`,
      response.status,
      body,
      path,
    );
  }
  return body;
}

export async function apiGet<P extends PathsWith<"get">>(
  path: P,
  options: GetOptions<Operation<P, "get">> = {} as GetOptions<Operation<P, "get">>,
): Promise<SuccessBody<Operation<P, "get">>> {
  return (await request("get", path, options)) as SuccessBody<Operation<P, "get">>;
}

export async function apiPost<P extends PathsWith<"post">>(
  path: P,
  options: BodyOptions<Operation<P, "post">>,
): Promise<SuccessBody<Operation<P, "post">>> {
  return (await request("post", path, options)) as SuccessBody<Operation<P, "post">>;
}
