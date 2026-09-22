import "server-only";

import { timingSafeEqual } from "node:crypto";

/**
 * The browser half of Google's authorisation-code flow, with PKCE.
 *
 *   state     - ties Google's redirect back to a sign-in this browser started.
 *               Without it, an attacker can complete *their* sign-in in the
 *               victim's browser (login CSRF) and harvest what the victim does.
 *   nonce     - ties the ID token to this sign-in; the API checks it.
 *   verifier  - PKCE. The authorisation code is useless without it, so a code
 *               leaked through a log or a Referer header cannot be redeemed.
 *
 * All three live in one short-lived httpOnly cookie scoped to /auth/google.
 */

export interface PendingSignIn {
  state: string;
  nonce: string;
  verifier: string;
  next: string;
}

export const GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth";
export const OAUTH_COOKIE_PATH = "/auth/google";
export const OAUTH_COOKIE_MAX_AGE = 10 * 60;

function base64url(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

/** `bytes` of CSPRNG output, base64url-encoded. 32 bytes -> 43 characters. */
export function randomToken(bytes: number): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** RFC 7636 S256: BASE64URL(SHA256(verifier)). */
export async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Constant-time string comparison, so `state` cannot be guessed by timing. */
export function safeEqual(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

/**
 * Where to send the user after sign-in. Only same-site paths.
 *
 * `next` arrives in a query string anyone can craft, so it is an open-redirect
 * vector: "//evil.example" and "/\\evil.example" are both treated by browsers
 * as another host. Anything that is not a plain absolute path falls back.
 */
export function safeNext(value: string | null | undefined, fallback = "/traces"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return fallback;
  }
  return value;
}

/**
 * The redirect URI Google sends the user back to. It must match, exactly, both
 * the URI registered in Google Cloud Console and one of the API's
 * AUTH_REDIRECT_URIS. APP_URL pins it when the request's own origin is not the
 * public one (a proxy in front of the app).
 */
export function callbackUrl(requestOrigin: string): string {
  const base = (process.env.APP_URL ?? requestOrigin).replace(/\/+$/, "");
  return `${base}/auth/google/callback`;
}

export function parsePending(raw: string | undefined): PendingSignIn | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<PendingSignIn>;
    if (value.state && value.nonce && value.verifier) {
      return { state: value.state, nonce: value.nonce, verifier: value.verifier, next: safeNext(value.next) };
    }
  } catch {
    // Malformed cookie: treat as no sign-in in progress.
  }
  return null;
}

/** State-changing routes accept only same-origin POSTs. */
export function isSameOrigin(request: Request, expectedOrigin: string): boolean {
  return request.headers.get("origin") === expectedOrigin;
}
