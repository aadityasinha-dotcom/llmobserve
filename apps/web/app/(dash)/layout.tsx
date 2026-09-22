import Link from "next/link";

import { ProjectSwitcher } from "@/components/project-switcher";
import { requireContext } from "@/lib/auth/session";

export const dynamic = "force-dynamic";

/**
 * The signed-in shell. `requireContext` is the real authentication check for
 * every dashboard page: it calls /v1/me, and a rejected session redirects
 * through /auth/expired before any page content renders.
 */
export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { me, project } = await requireContext();

  return (
    <div className="min-h-screen bg-background text-foreground">
      <nav className="flex items-center gap-4 border-b border-border px-4 py-2 text-xs">
        <Link href="/traces" className="font-semibold tracking-tight">
          llm-observe
        </Link>
        <ProjectSwitcher projects={me.projects} current={project.id} next="/traces" />
        <Link href="/traces" className="text-muted-foreground hover:text-foreground">
          Traces
        </Link>
        <Link href="/settings/keys" className="text-muted-foreground hover:text-foreground">
          API keys
        </Link>
        <span className="ml-auto truncate text-muted-foreground" title={me.user.email}>
          {me.user.email}
        </span>
        <form method="post" action="/auth/logout">
          <button type="submit" className="text-muted-foreground hover:text-foreground">
            Sign out
          </button>
        </form>
      </nav>
      {children}
    </div>
  );
}
