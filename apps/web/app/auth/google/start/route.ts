import { NextResponse, type NextRequest } from "next/server";

import { getGoogleClientId, MissingApiConfigError } from "@/lib/api";
import { isSecureOrigin, OAUTH_COOKIE } from "@/lib/auth/cookies";
import {
  callbackUrl,
  GOOGLE_AUTHORIZE_URL,
  OAUTH_COOKIE_MAX_AGE,
  OAUTH_COOKIE_PATH,
  pkceChallenge,
  randomToken,
  safeNext,
} from "@/lib/auth/oauth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Begin a Google sign-in: mint state/nonce/PKCE, remember them, redirect. */
export async function GET(request: NextRequest) {
  let clientId: string;
  try {
    clientId = getGoogleClientId();
  } catch (error) {
    if (error instanceof MissingApiConfigError) {
      return NextResponse.redirect(new URL("/login?error=unconfigured", request.url));
    }
    throw error;
  }

  const state = randomToken(32);
  const nonce = randomToken(32);
  // 48 bytes -> 64 characters, inside RFC 7636's 43..128.
  const verifier = randomToken(48);
  const next = safeNext(request.nextUrl.searchParams.get("next"));

  const authorize = new URL(GOOGLE_AUTHORIZE_URL);
  authorize.search = new URLSearchParams({
    client_id: clientId,
    redirect_uri: callbackUrl(request.nextUrl.origin),
    response_type: "code",
    scope: "openid email profile",
    state,
    nonce,
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: "S256",
    // Always show the account chooser, so "sign out, sign in as someone
    // else" works without also signing out of Google.
    prompt: "select_account",
  }).toString();

  const response = NextResponse.redirect(authorize);
  response.cookies.set(OAUTH_COOKIE, JSON.stringify({ state, nonce, verifier, next }), {
    httpOnly: true,
    secure: isSecureOrigin(request.url),
    sameSite: "lax", // must survive the top-level redirect back from Google
    path: OAUTH_COOKIE_PATH,
    maxAge: OAUTH_COOKIE_MAX_AGE,
  });
  return response;
}
