import Link from "next/link";

import type { ScoreDetail } from "@/lib/api/types";
import { formatScoreValue, formatTimestamp, shortId } from "@/lib/format";

/**
 * Every score on a trace, as a table.
 *
 * Scores are the loop a cost ledger cannot close: a user's thumbs-down, a
 * heuristic check, a judge model's rating. They arrive with no foreign key to
 * their subject, so a score may name an observation that is not in the tree
 * yet; the target column then shows the id rather than nothing.
 */
export function ScoreTable({
  scores,
  observationNames,
}: {
  scores: ScoreDetail[];
  observationNames: Map<string, string | null | undefined>;
}) {
  if (scores.length === 0) return null;

  return (
    <section className="border-b border-border">
      <h2 className="px-4 pt-3 text-[11px] uppercase tracking-wider text-muted-foreground">
        Scores <span className="font-mono">({scores.length})</span>
      </h2>
      <table className="mt-1 w-full border-collapse text-xs">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-muted-foreground">
            <th className="px-4 py-1.5 font-medium">Name</th>
            <th className="px-3 py-1.5 font-medium">Value</th>
            <th className="px-3 py-1.5 font-medium">Source</th>
            <th className="px-3 py-1.5 font-medium">On</th>
            <th className="px-3 py-1.5 font-medium">Comment</th>
            <th className="px-3 py-1.5 font-medium whitespace-nowrap">Scored</th>
          </tr>
        </thead>
        <tbody>
          {scores.map((score) => (
            <tr key={score.id} className="border-t border-border/60">
              <td className="px-4 py-1.5 font-medium">{score.name}</td>
              <td className="px-3 py-1.5 font-mono tabular-nums">
                <ScoreValue score={score} />
              </td>
              <td className="px-3 py-1.5 font-mono text-muted-foreground">{score.source}</td>
              <td className="px-3 py-1.5 font-mono text-muted-foreground">
                {score.observation_id ? (
                  <span title={score.observation_id}>
                    {observationNames.get(score.observation_id) ?? shortId(score.observation_id)}
                  </span>
                ) : (
                  "trace"
                )}
              </td>
              <td className="max-w-md truncate px-3 py-1.5 text-muted-foreground">
                {score.comment ?? ""}
              </td>
              <td className="px-3 py-1.5 font-mono text-muted-foreground whitespace-nowrap">
                {formatTimestamp(score.scored_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/**
 * Colour carries meaning here and nowhere else in the row: a boolean "no" or
 * a negative number is the thing an engineer is scanning for.
 */
export function ScoreValue({ score }: { score: ScoreDetail }) {
  const text = formatScoreValue(score);
  const negative =
    (score.data_type === "boolean" && text === "no") ||
    (score.data_type === "numeric" && Number(score.value) < 0);
  return <span className={negative ? "text-destructive" : undefined}>{text}</span>;
}

/** Inline chips for the scores attached to one observation. */
export function ScoreBadges({ scores }: { scores: ScoreDetail[] }) {
  if (scores.length === 0) return null;
  return (
    <>
      {scores.map((score) => (
        <span
          key={score.id}
          title={`${score.name} = ${formatScoreValue(score)} (${score.source})${score.comment ? `: ${score.comment}` : ""}`}
          className="ml-2 rounded border border-border px-1 font-mono text-[10px] text-muted-foreground"
        >
          {score.name} <ScoreValue score={score} />
        </span>
      ))}
    </>
  );
}

/** A tag rendered as a link to the list filtered by it. */
export function TagChip({ tag }: { tag: string }) {
  return (
    <Link
      href={`/traces?tag=${encodeURIComponent(tag)}`}
      className="rounded border border-border px-1 font-mono text-[10px] text-muted-foreground hover:text-foreground"
    >
      {tag}
    </Link>
  );
}
