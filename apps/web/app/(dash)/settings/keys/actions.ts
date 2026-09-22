"use server";

import { revalidatePath } from "next/cache";

import { ApiError, apiDelete, apiPost } from "@/lib/api";
import { requireContext } from "@/lib/auth/session";

/**
 * Server Actions for key management. Next checks the Origin header on every
 * Server Action, which is the CSRF protection here; the session cookie is
 * SameSite=Lax on top.
 *
 * The project comes from the signed-in context, never from the form. A hidden
 * project_id field would be editable, and while the API would refuse a project
 * the user does not belong to, there is no reason to offer the choice.
 */

export type CreateKeyState =
  | { status: "idle" }
  | { status: "created"; apiKey: string; label: string | null; scopes: string[] }
  | { status: "error"; message: string };

const SCOPES = new Set(["ingest", "read"]);

export async function createKeyAction(
  _previous: CreateKeyState,
  form: FormData,
): Promise<CreateKeyState> {
  const { project } = await requireContext();
  const scopes = form.getAll("scopes").map(String).filter((s) => SCOPES.has(s));
  const label = String(form.get("label") ?? "").trim() || null;

  if (scopes.length === 0) {
    return { status: "error", message: "Choose at least one scope." };
  }

  try {
    const created = await apiPost("/v1/projects/{project_id}/keys", {
      path: { project_id: project.id },
      body: { label, scopes: scopes as ("ingest" | "read")[] },
    });
    revalidatePath("/settings/keys");
    // The only time the raw key exists outside the API's memory. It is handed
    // to the client component that displays it and is not stored anywhere.
    return { status: "created", apiKey: created.api_key, label, scopes };
  } catch (error) {
    const message =
      error instanceof ApiError ? `Could not create the key (${error.status}).` : "Could not create the key.";
    return { status: "error", message };
  }
}

export async function revokeKeyAction(form: FormData): Promise<void> {
  const { project } = await requireContext();
  const keyId = String(form.get("key_id") ?? "");
  await apiDelete("/v1/projects/{project_id}/keys/{key_id}", {
    path: { project_id: project.id, key_id: keyId },
  });
  revalidatePath("/settings/keys");
}
