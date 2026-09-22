import { NextResponse, type NextRequest } from "next/server";

import { ApiError, apiPost } from "@/lib/api";
import {
  isSecureOrigin,
  OAUTH_COOKIE,
  PROJECT_COOKIE,
  SESSION_COOKIE,
} from "@/lib/auth/cookies";
import { callbackUrl, OAUTH_COOKIE_PATH, parsePending, safeEqual } from "@/lib/auth/oauth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Google redirects here. Check `state`, then hand the code to the API, which
 * exchanges it with Google, verifies the ID token, and returns a session.
 */
export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const pending = parsePending(request.cookies.get(OAUTH_COOKIE)?.value);

  const finish = (location: string) => {
    const response = NextResponse.redirect(new URL(location, request.url));
    // One use only, whatever the outcome.
    response.cookies.set(OAUTH_COOKIE, "", { path: OAUTH_COOKIE_PATH, maxAge: 0 });
    return response;
  };

  // The user pressed Cancel on Google's screen, or Google refused the request.
  if (params.get("error")) return finish("/login?error=cancelled");
  // No sign-in in progress from this browser: expired, or a forged callback.
  if (!pending) return finish("/login?error=expired");

  const state = params.get("state") ?? "";
  const code = params.get("code") ?? "";
  if (!state || !safeEqual(state, pending.state) || !code) {
    return finish("/login?error=state");
  }

  let session;
  try {
    session = await apiPost("/v1/auth/google", {
      anonymous: true,
      body: {
        code,
        code_verifier: pending.verifier,
        redirect_uri: callbackUrl(request.nextUrl.origin),
        nonce: pending.nonce,
      },
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) return finish("/login?error=forbidden");
    if (error instanceof ApiError && error.status === 503) return finish("/login?error=unconfigured");
    console.error("sign-in exchange failed:", error instanceof Error ? error.message : error);
    return finish("/login?error=failed");
  }

  // A brand-new account has no traces and no key yet: send it to key setup,
  // which is the only useful next step.
  const response = finish(session.created ? "/settings/keys?welcome=1" : pending.next);
  response.cookies.set(SESSION_COOKIE, session.session_token, {
    httpOnly: true,
    secure: isSecureOrigin(request.url),
    sameSite: "lax",
    path: "/",
    expires: new Date(session.expires_at),
  });
  // A previous user's project choice must not carry into this session.
  response.cookies.set(PROJECT_COOKIE, "", { path: "/", maxAge: 0 });
  return response;
}
