import "server-only";

import { cookies } from "next/headers";

/**
 * The dashboard's cookies. All httpOnly: JavaScript in the page cannot read
 * them, so an XSS bug cannot exfiltrate a session.
 *
 * SameSite=Lax is what protects the state-changing routes (sign-out, project
 * switch, key creation) from cross-site request forgery: a form on another site
 * posting here does not carry the cookie. Server Actions add an Origin check on
 * top of that.
 */

/** The API-issued session token, forwarded as `Authorization: Bearer`. */
export const SESSION_COOKIE = "llmo_session";
/** Which of the user's projects is selected. A preference, not a credential:
 * the API re-checks membership on every request, so forging it gains nothing. */
export const PROJECT_COOKIE = "llmo_project";
/** state, nonce and PKCE verifier for one in-flight sign-in. Ten minutes. */
export const OAUTH_COOKIE = "llmo_oauth";

export async function getSessionToken(): Promise<string | undefined> {
  return (await cookies()).get(SESSION_COOKIE)?.value;
}

export async function getSelectedProjectId(): Promise<string | undefined> {
  return (await cookies()).get(PROJECT_COOKIE)?.value;
}

/** Secure cookies need HTTPS; localhost development is plain HTTP. */
export function isSecureOrigin(url: string | URL): boolean {
  return new URL(url).protocol === "https:";
}
