import { NextResponse, type NextRequest } from "next/server";

/**
 * Send signed-out visitors to /login before any dashboard page renders.
 *
 * This checks only that a session cookie is present - middleware cannot
 * verify it without a round trip to the API on every asset request. The real
 * check happens in the page, where /v1/me either succeeds or redirects through
 * /auth/expired. So this is a fast path for the obvious case, not the lock.
 */
export function middleware(request: NextRequest) {
  if (request.cookies.has("llmo_session")) return NextResponse.next();

  const login = new URL("/login", request.url);
  const destination = request.nextUrl.pathname + request.nextUrl.search;
  if (destination !== "/") login.searchParams.set("next", destination);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/", "/traces/:path*", "/settings/:path*"],
};
