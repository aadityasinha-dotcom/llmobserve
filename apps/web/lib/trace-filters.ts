/**
 * The trace-list filters, as they appear in the dashboard URL.
 *
 * One definition shared by the server page (which reads them and forwards
 * them to the API), the filter form (which writes them), and the proxy route
 * (which allow-lists them). A filter added in one place and not the others
 * silently does nothing, which is exactly the kind of drift a single list
 * prevents.
 */

/** Exact-match and range filters. One value each. */
export const SCALAR_FILTERS = [
  "name",
  "user_id",
  "session_id",
  "environment",
  "release",
  "from",
  "to",
] as const;

export type ScalarFilter = (typeof SCALAR_FILTERS)[number];

/** Repeatable: every `tag` must be present on a trace for it to match. */
export const TAG_FILTER = "tag";

export type TraceFilterValues = Partial<Record<ScalarFilter, string>> & {
  tags: string[];
};

type Raw = Record<string, string | string[] | undefined>;

function last(value: string | string[] | undefined): string | undefined {
  if (Array.isArray(value)) return value[value.length - 1];
  return value ?? undefined;
}

function all(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return (Array.isArray(value) ? value : [value]).filter((v) => v !== "");
}

/** Pull the filters out of a parsed search-params object. */
export function readFilters(params: Raw): TraceFilterValues {
  const values: TraceFilterValues = { tags: all(params[TAG_FILTER]) };
  for (const key of SCALAR_FILTERS) {
    const value = last(params[key]);
    if (value) values[key] = value;
  }
  return values;
}

export function hasAnyFilter(values: TraceFilterValues): boolean {
  return values.tags.length > 0 || SCALAR_FILTERS.some((key) => Boolean(values[key]));
}

/** Write the filters back into a URLSearchParams, dropping empty ones. */
export function writeFilters(values: TraceFilterValues, into: URLSearchParams): void {
  for (const key of SCALAR_FILTERS) {
    const value = values[key];
    if (value) into.set(key, value);
  }
  for (const tag of values.tags) into.append(TAG_FILTER, tag);
}

/** Tags are typed as one comma-separated field; the URL carries them apart. */
export function parseTagInput(text: string): string[] {
  const seen: string[] = [];
  for (const raw of text.split(",")) {
    const tag = raw.trim();
    if (tag && !seen.includes(tag)) seen.push(tag);
  }
  return seen;
}
