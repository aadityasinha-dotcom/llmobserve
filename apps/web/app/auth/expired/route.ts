import { NextResponse, type NextRequest } from "next/server";

import { PROJECT_COOKIE, SESSION_COOKIE } from "@/lib/auth/cookies";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Where a page sends the user when the API rejects their session.
 *
 * Server components cannot modify cookies, so they cannot clear a dead session
 * themselves. Bouncing through here clears it before /login, which otherwise
 * would keep the stale cookie and loop.
 *
 * GET, so a forged request can at worst sign someone out - the one
 * state change that needs no protection.
 */
export function GET(request: NextRequest) {
  const response = NextResponse.redirect(new URL("/login?error=expired", request.url));
  response.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  response.cookies.set(PROJECT_COOKIE, "", { path: "/", maxAge: 0 });
  return response;
}
