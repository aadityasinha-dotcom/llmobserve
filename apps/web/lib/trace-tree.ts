import type { ObservationDetail } from "@/lib/api/types";

export interface SpanNode {
  observation: ObservationDetail;
  depth: number;
  children: SpanNode[];
  /** True when this node names a parent that is not in the response. */
  orphaned: boolean;
}

/**
 * Rebuild the observation tree from the flat list the API returns.
 *
 * The API deliberately does not nest these: `parent_observation_id` carries no
 * foreign key in Postgres, because batches split and retry independently and an
 * FK would turn late delivery of a parent into a rejected child. The cost of
 * that choice is paid here — the tree has to tolerate a shape the database
 * never validated.
 *
 * Three defences, each for a case the backend cannot rule out:
 *
 * * **Dangling parent.** A parent still in flight is normal. Its children
 *   surface at the root, flagged, rather than disappearing — a span that is
 *   silently dropped from a debugging view is worse than one shown out of place.
 * * **Cycles.** Nothing stops a client from sending A→B→A. Walking up from each
 *   node before attaching it keeps a malformed batch from hanging the render.
 * * **Duplicate ids.** The first occurrence wins, so a repeat cannot appear
 *   twice in the tree or overwrite a node that already has children.
 */
export function buildSpanTree(observations: ObservationDetail[]): SpanNode[] {
  const nodes = new Map<string, SpanNode>();
  for (const observation of observations) {
    if (nodes.has(observation.id)) continue;
    nodes.set(observation.id, {
      observation,
      depth: 0,
      children: [],
      orphaned: false,
    });
  }

  /** True if attaching `id` under `parentId` would close a loop. */
  function wouldCycle(id: string, parentId: string): boolean {
    const seen = new Set<string>([id]);
    let cursor: string | null | undefined = parentId;
    while (cursor) {
      if (seen.has(cursor)) return true;
      seen.add(cursor);
      cursor = nodes.get(cursor)?.observation.parent_observation_id;
    }
    return false;
  }

  const roots: SpanNode[] = [];
  for (const node of nodes.values()) {
    const parentId = node.observation.parent_observation_id;
    const parent = parentId ? nodes.get(parentId) : undefined;

    if (!parentId) {
      roots.push(node);
    } else if (!parent) {
      node.orphaned = true;
      roots.push(node);
    } else if (wouldCycle(node.observation.id, parentId)) {
      node.orphaned = true;
      roots.push(node);
    } else {
      parent.children.push(node);
    }
  }

  // Depth is assigned by walking down, not while attaching: a child can be
  // linked before its parent has found its own place in the tree.
  function assignDepth(node: SpanNode, depth: number): void {
    node.depth = depth;
    node.children.sort(byStartTime);
    for (const child of node.children) assignDepth(child, depth + 1);
  }
  roots.sort(byStartTime);
  for (const root of roots) assignDepth(root, 0);

  return roots;
}

function byStartTime(a: SpanNode, b: SpanNode): number {
  const delta =
    new Date(a.observation.started_at).getTime() -
    new Date(b.observation.started_at).getTime();
  // Ties broken by id so the render order is stable across reloads.
  return delta !== 0 ? delta : a.observation.id.localeCompare(b.observation.id);
}

/** Depth-first flattening, which is the order the tree is rendered in. */
export function flattenTree(roots: SpanNode[]): SpanNode[] {
  const out: SpanNode[] = [];
  const walk = (node: SpanNode) => {
    out.push(node);
    for (const child of node.children) walk(child);
  };
  for (const root of roots) walk(root);
  return out;
}
