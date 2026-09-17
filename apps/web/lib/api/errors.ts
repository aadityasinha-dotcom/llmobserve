/**
 * A non-2xx response from the backend, or a transport failure reaching it.
 *
 * Carries the parsed body so callers can surface a backend validation message
 * instead of a generic failure. Safe to import from client code: it holds no
 * credentials and no config.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
    readonly path: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** No HTTP response at all — DNS, connection refused, timeout. */
  static transport(path: string, cause: unknown): ApiError {
    const detail = cause instanceof Error ? cause.message : String(cause);
    return new ApiError(
      `Could not reach the backend at ${path}: ${detail}`,
      0,
      null,
      path,
    );
  }

  /** True when the backend rejected our API key. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}
