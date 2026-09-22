import { apiGet, getApiConfig } from "@/lib/api";
import { formatRelative, formatTimestamp } from "@/lib/format";
import { requireContext } from "@/lib/auth/session";

import { revokeKeyAction } from "./actions";
import { CreateKeyForm } from "./create-key-form";

export const dynamic = "force-dynamic";

export default async function KeysPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { project } = await requireContext();
  const welcome = (await searchParams).welcome === "1";
  const keys = await apiGet("/v1/projects/{project_id}/keys", {
    path: { project_id: project.id },
  });
  const active = keys.filter((k) => !k.revoked_at);
  const revoked = keys.filter((k) => k.revoked_at);

  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <h1 className="text-sm font-semibold tracking-tight">API keys</h1>
      <p className="mt-1 text-xs text-muted-foreground">
        Keys for <span className="font-mono">{project.name}</span>. Traces sent with any of them
        appear only in this project, and only people who belong to it can see them.
      </p>

      {welcome && (
        <div className="mt-4 rounded border border-border bg-muted/30 p-3 text-xs">
          <p className="font-medium">Your account and project are ready.</p>
          <p className="mt-1 text-muted-foreground">
            Create an <span className="font-mono">ingest</span> key below and give it to the SDK.
            The first trace you send will appear under Traces.
          </p>
        </div>
      )}

      <section className="mt-6">
        <CreateKeyForm apiBaseUrl={getApiConfig().baseUrl} />
      </section>

      <section className="mt-8">
        <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground">Active</h2>
        {active.length === 0 ? (
          <p className="mt-2 text-xs text-muted-foreground">No active keys.</p>
        ) : (
          <KeyTable keys={active} revocable />
        )}
      </section>

      {revoked.length > 0 && (
        <section className="mt-8">
          <h2 className="text-[11px] uppercase tracking-wider text-muted-foreground">Revoked</h2>
          <KeyTable keys={revoked} revocable={false} />
        </section>
      )}
    </div>
  );
}

function KeyTable({
  keys,
  revocable,
}: {
  keys: Awaited<ReturnType<typeof apiGet<"/v1/projects/{project_id}/keys">>>;
  revocable: boolean;
}) {
  return (
    <table className="mt-2 w-full border-collapse text-xs">
      <thead>
        <tr className="border-b border-border text-left text-[11px] uppercase tracking-wider text-muted-foreground">
          <th className="py-2 pr-3 font-medium">Key</th>
          <th className="py-2 pr-3 font-medium">Label</th>
          <th className="py-2 pr-3 font-medium">Scopes</th>
          <th className="py-2 pr-3 font-medium">Created</th>
          <th className="py-2 font-medium" />
        </tr>
      </thead>
      <tbody>
        {keys.map((key) => (
          <tr key={key.id} className="border-b border-border/60">
            <td className="py-2 pr-3 font-mono">{key.key_prefix ?? "—"}…</td>
            <td className="py-2 pr-3">{key.label ?? <span className="text-muted-foreground">—</span>}</td>
            <td className="py-2 pr-3 font-mono">{key.scopes.join(", ")}</td>
            <td className="py-2 pr-3" title={formatTimestamp(key.created_at)}>
              {formatRelative(key.created_at)}
            </td>
            <td className="py-2 text-right">
              {revocable ? (
                <form action={revokeKeyAction}>
                  <input type="hidden" name="key_id" value={key.id} />
                  <button type="submit" className="text-destructive hover:underline">
                    Revoke
                  </button>
                </form>
              ) : (
                <span className="text-muted-foreground" title={formatTimestamp(key.revoked_at!)}>
                  revoked {formatRelative(key.revoked_at!)}
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
