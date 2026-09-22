import { NextResponse, type NextRequest } from "next/server";

import { isSecureOrigin, PROJECT_COOKIE } from "@/lib/auth/cookies";
import { isSameOrigin, safeNext } from "@/lib/auth/oauth";
import { requireMe } from "@/lib/auth/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Switch the selected project. Only to one the user actually belongs to. */
export async function POST(request: NextRequest) {
  if (!isSameOrigin(request, request.nextUrl.origin)) {
    return new NextResponse("Cross-origin request refused", { status: 403 });
  }
  const form = await request.formData();
  const projectId = String(form.get("project_id") ?? "");
  const next = safeNext(String(form.get("next") ?? ""));

  // Validated against the API's view of the user's memberships. The API would
  // refuse a forged id anyway; checking here keeps a bad value out of the
  // cookie so the next page does not render an error.
  const me = await requireMe();
  const response = NextResponse.redirect(new URL(next, request.url), 303);
  if (me.projects.some((p) => p.id === projectId)) {
    response.cookies.set(PROJECT_COOKIE, projectId, {
      httpOnly: true,
      secure: isSecureOrigin(request.url),
      sameSite: "lax",
      path: "/",
      maxAge: 60 * 60 * 24 * 365,
    });
  }
  return response;
}
