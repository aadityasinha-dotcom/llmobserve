import "server-only";

/**
 * Backend connection details.
 *
 * Both values are server-only. `server-only` above makes importing this from a
 * client component a build error, which is the enforcement behind "the API key
 * never reaches the browser" — neither var is `NEXT_PUBLIC_`, so they are not
 * inlined into the client bundle.
 */
export interface ApiConfig {
  baseUrl: string;
  apiKey: string;
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

  const apiKey = process.env.LLMOBSERVE_API_KEY;
  if (!apiKey) throw new MissingApiConfigError("LLMOBSERVE_API_KEY");

  // Trailing slash here plus a leading slash on every path would produce "//".
  return { baseUrl: baseUrl.replace(/\/+$/, ""), apiKey };
}
