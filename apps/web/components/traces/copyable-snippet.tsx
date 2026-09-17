"use client";

import { useState } from "react";

/**
 * A code block with a copy button.
 *
 * Client-side only because the clipboard is. Falls back to showing the code
 * plainly if the write fails — in a non-secure context navigator.clipboard is
 * undefined, and the snippet is still selectable by hand.
 */
export function CopyableSnippet({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Nothing to recover: the code is on screen and selectable.
    }
  }

  return (
    <div className="relative mt-4">
      <pre className="overflow-x-auto rounded border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
        {code}
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute right-2 top-2 rounded border border-border bg-background px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
