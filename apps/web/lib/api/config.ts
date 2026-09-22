import "server-only";

/**
 * Backend connection details.
 *
 * The dashboard holds no API key any more. It authenticates each request as the
 * signed-in user, with the session token the API issued at sign-in (see
 * lib/auth). What remains here is where the API is, and the Google OAuth client
 * id needed to build the sign-in redirect - a public identifier, not a secret.
 * Google's client *secret* lives only in the API.
 *
 * `server-only` makes importing this from a client component a build error.
 */
export interface ApiConfig {
  baseUrl: string;
}

export class MissingApiConfigError extends Error {
  constructor(readonly variable: string) {
    super(
      `${variable} is not set. Copy .env.example to .env.local and fill it in.`,
    );
    this.name = "MissingApiConfigError";
  }
}

export function getApiConfig(): ApiConfig {
  const baseUrl = process.env.API_BASE_URL;
  if (!baseUrl) throw new MissingApiConfigError("API_BASE_URL");
  // Trailing slash here plus a leading slash on every path would produce "//".
  return { baseUrl: baseUrl.replace(/\/+$/, "") };
}

export function getGoogleClientId(): string {
  const clientId = process.env.GOOGLE_CLIENT_ID;
  if (!clientId) throw new MissingApiConfigError("GOOGLE_CLIENT_ID");
  return clientId;
}
