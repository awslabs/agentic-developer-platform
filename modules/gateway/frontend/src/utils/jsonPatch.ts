/**
 * Minimal RFC-6902 JSON Patch application for AG-UI STATE_DELTA events.
 *
 * Issue #4208: the previous inline handling did `op.path.replace(/^\//, '')` and
 * used the result as a single top-level key, so any nested JSON Pointer (e.g.
 * `/draft/intent`) was applied as a literal key named `draft/intent` and never
 * rendered — a silent no-op that looked like a broken panel. This resolves
 * pointers properly, including RFC-6901 token unescaping (`~1` → `/`,
 * `~0` → `~`).
 *
 * Scope is deliberately narrow: add / replace / remove against plain objects and
 * arrays, which is all the worker emits. Array `-` (append) is supported since
 * it is the natural way to push onto a list.
 */

export interface JsonPatchOp {
  op: string;
  path: string;
  value?: unknown;
}

/** Unescape one RFC-6901 reference token. `~1` → `/` and `~0` → `~`, in that order. */
export function unescapePointerToken(token: string): string {
  return token.replace(/~1/g, '/').replace(/~0/g, '~');
}

/** Split a JSON Pointer into its decoded reference tokens. `''` => []. */
export function parsePointer(path: string): string[] {
  if (path === '' || path === '/') return [];
  const raw = path.startsWith('/') ? path.slice(1) : path;
  return raw.split('/').map(unescapePointerToken);
}

function isIndexToken(token: string): boolean {
  return /^(0|[1-9][0-9]*)$/.test(token);
}

/**
 * Apply one patch operation to a copy of `target`, returning the new value.
 *
 * Unknown ops and unresolvable paths return the input unchanged rather than
 * throwing — a malformed event from the worker must not take down the chat UI.
 */
export function applyPatch<T extends Record<string, unknown>>(target: T, op: JsonPatchOp): T {
  const tokens = parsePointer(op.path);
  if (tokens.length === 0) return target;
  if (op.op !== 'add' && op.op !== 'replace' && op.op !== 'remove') return target;

  const root: Record<string, unknown> = { ...target };

  // Walk to the parent of the target location, shallow-copying each container
  // along the way so React sees a new reference at every mutated level.
  let parent: Record<string, unknown> | unknown[] = root;
  for (let i = 0; i < tokens.length - 1; i++) {
    const token = tokens[i];
    const container: unknown = Array.isArray(parent)
      ? (parent as unknown[])[Number(token)]
      : (parent as Record<string, unknown>)[token];

    let next: Record<string, unknown> | unknown[];
    if (Array.isArray(container)) {
      next = [...container];
    } else if (container !== null && typeof container === 'object') {
      next = { ...(container as Record<string, unknown>) };
    } else if (op.op === 'remove') {
      // Nothing to remove along a path that does not exist.
      return target;
    } else {
      // Vivify missing intermediate containers so a nested `add` works against
      // a draft that has not been written yet. Numeric next token => array.
      next = isIndexToken(tokens[i + 1]) ? [] : {};
    }

    if (Array.isArray(parent)) {
      const idx = Number(token);
      if (!Number.isInteger(idx)) return target;
      (parent as unknown[])[idx] = next;
    } else {
      (parent as Record<string, unknown>)[token] = next;
    }
    parent = next;
  }

  const leaf = tokens[tokens.length - 1];

  if (Array.isArray(parent)) {
    const arr = parent as unknown[];
    if (op.op === 'remove') {
      if (!isIndexToken(leaf)) return target;
      arr.splice(Number(leaf), 1);
    } else if (leaf === '-') {
      arr.push(op.value);
    } else if (isIndexToken(leaf)) {
      const idx = Number(leaf);
      if (op.op === 'add') arr.splice(idx, 0, op.value);
      else arr[idx] = op.value;
    } else {
      return target;
    }
  } else {
    const obj = parent as Record<string, unknown>;
    if (op.op === 'remove') delete obj[leaf];
    else obj[leaf] = op.value;
  }

  return root as T;
}

/** Apply a sequence of patch ops in order. */
export function applyPatches<T extends Record<string, unknown>>(
  target: T,
  ops: readonly JsonPatchOp[],
): T {
  return ops.reduce<T>((acc, op) => applyPatch(acc, op), target);
}
