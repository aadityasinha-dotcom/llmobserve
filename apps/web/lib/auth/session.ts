import "server-only";

import { redirect } from "next/navigation";
import { cache } from "react";

import { ApiError, apiGet } from "@/lib/api";
import type { Me, Project } from "@/lib/api/types";

import { getSelectedProjectId } from "./cookies";

/**
 * The signed-in user, or a redirect to sign-in.
 *
 * `cache` dedupes it per request: the layout and the page both need it, and
 * without this each would make its own round trip to /v1/me.
 *
 * A 401 goes to /auth/expired rather than straight to /login. That route
 * clears the stale cookie first; redirecting to /login with the cookie still
 * set would leave a dead session in place for the next page load.
 */
export const requireMe = cache(async (): Promise<Me> => {
  try {
    return await apiGet("/v1/me");
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      redirect("/auth/expired");
    }
    throw error;
  }
});

/**
 * The project this page is about: the one chosen in the switcher if the user
 * still belongs to it, otherwise their first (the personal one made at
 * sign-up). The cookie is a preference; membership comes from the API.
 */
export async function currentProject(me: Me): Promise<Project> {
  const chosen = await getSelectedProjectId();
  const project = me.projects.find((p) => p.id === chosen) ?? me.projects[0];
  if (!project) {
    // Every account is created with a personal project, so this means the
    // user's memberships were all removed after sign-up.
    throw new Error("Your account has no projects. Ask an owner to add you to one.");
  }
  return project;
}

/** Everything a dashboard page needs about who is looking, and at what. */
export async function requireContext(): Promise<{ me: Me; project: Project }> {
  const me = await requireMe();
  return { me, project: await currentProject(me) };
}

/** The header that tells the API which project a request is about. */
export function projectHeader(project: Project): Record<string, string> {
  return { "x-project-id": project.id };
}
