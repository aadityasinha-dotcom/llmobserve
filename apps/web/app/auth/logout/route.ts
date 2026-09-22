import { NextResponse, type NextRequest } from "next/server";

import { apiPost } from "@/lib/api";
import { PROJECT_COOKIE, SESSION_COOKIE } from "@/lib/auth/cookies";
import { isSameOrigin } from "@/lib/auth/oauth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Sign out. POST only, same-origin only: a GET sign-out can be triggered by an
 * <img> tag on any page the user visits.
 */
export async function POST(request: NextRequest) {
  if (!isSameOrigin(request, request.nextUrl.origin)) {
    return new NextResponse("Cross-origin sign-out refused", { status: 403 });
  }

  // Invalidate the session server-side first, on every device. Best effort:
  // if the API is unreachable the cookie is still cleared below, and the token
  // expires on its own.
  try {
    await apiPost("/v1/auth/logout", { body: undefined });
  } catch (error) {
    console.error("server-side sign-out failed:", error instanceof Error ? error.message : error);
  }

  const response = NextResponse.redirect(new URL("/login?signed_out=1", request.url), 303);
  response.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  response.cookies.set(PROJECT_COOKIE, "", { path: "/", maxAge: 0 });
  return response;
}
