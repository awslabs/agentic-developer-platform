/**
 * Per-journey return location — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * The acceptance criteria ask for "a per-journey return location", and the design
 * note's step 2 asks that navigation state survive refresh and deep links and
 * "remember the last page in each journey". So switching from Administration back
 * to Use ADP should return you where you were in Use ADP, not to its home page,
 * and that should still be true after a reload.
 *
 * ## Why this is storage-backed, and why that needs care
 *
 * Surviving a refresh means persisting. But the same criteria forbid leaking prior
 * tenant state across an organization switch, and persistence is exactly what makes
 * that possible: switching organization is a hard `window.location.assign('/')`
 * (see `services/workspaces.ts`), which destroys all in-memory state for free —
 * anything written to storage, by contrast, survives it. A naive
 * `lastPage`-per-journey key would therefore carry one tenant's position into
 * another tenant's session.
 *
 * Three rules make that safe:
 *
 * 1. **Keyed by user AND organization.** An entry recorded under one org is
 *    unreadable under another, so a switch cannot restore a prior tenant's
 *    location even though the value is still on disk.
 * 2. **Other-tenant entries are pruned on write.** The store keeps only the
 *    current key, so a long-lived session that visits many organizations does not
 *    accumulate a map of where the person has been in each. This bounds the record
 *    as well as hiding it.
 * 3. **`sessionStorage`, not `localStorage`.** Per-tab and cleared when the browser
 *    session ends. Navigation convenience does not warrant a durable cross-session
 *    record of a user's position, and a shared device should not surface it.
 *
 * A remembered path is additionally validated by the *caller* against the live
 * gated journey model (`isRestorablePath`) before being used, so a permission
 * revoked since the location was stored cannot send the user back to a page they
 * may no longer see. This hook is the storage layer; it does not decide
 * authorization.
 *
 * Storage is treated as optional throughout: Safari private mode and
 * storage-partitioned contexts throw on access. Every failure degrades to "no
 * memory", which costs the user a return to the journey home page and nothing
 * else. Navigation must never break because storage is unavailable.
 */

import { useCallback, useMemo } from 'react';
import type { JourneyId } from '@/components/next/journeys';

/** Storage key. Versioned so a future shape change cannot misread old values. */
const STORAGE_KEY = 'adp.next.journeyMemory.v1';

type JourneyMemory = Partial<Record<JourneyId, string>>;

interface StoredMemory {
  /** Identity of the tenant+user this memory belongs to. */
  scope: string;
  journeys: JourneyMemory;
}

/**
 * Identity of the (user, organization) pair a memory belongs to.
 *
 * Both parts matter: the user because a shared device may sign in as someone else
 * without clearing the tab's session storage, and the organization because the same
 * user's position in one tenant is not their position in another.
 */
function scopeKey(userId: string | undefined, orgId: string | undefined): string {
  return `${userId ?? 'anonymous'}::${orgId ?? 'no-org'}`;
}

/** `sessionStorage` if it is usable, else null. Never throws. */
function storage(): Storage | null {
  try {
    return window.sessionStorage ?? null;
  } catch {
    // Access itself throws in some partitioned/private contexts.
    return null;
  }
}

/** Read the memory for `scope`. Any other scope, or any unreadable value, is
 *  treated as no memory — this is the tenant-isolation read barrier. */
function read(scope: string): JourneyMemory {
  const store = storage();
  if (!store) return {};
  try {
    const raw = store.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as StoredMemory | null;
    // A value belonging to another user or organization is not ours to read.
    if (!parsed || typeof parsed !== 'object' || parsed.scope !== scope) return {};
    const journeys = parsed.journeys;
    if (!journeys || typeof journeys !== 'object') return {};
    return journeys;
  } catch {
    // Malformed JSON from an older build, or a storage read failure.
    return {};
  }
}

/** Replace the stored memory with `journeys` under `scope`, discarding any entry
 *  belonging to another scope. */
function write(scope: string, journeys: JourneyMemory): void {
  const store = storage();
  if (!store) return;
  try {
    // Writing the whole object rather than merging is what prunes other tenants:
    // there is only ever one scope's memory on disk.
    store.setItem(STORAGE_KEY, JSON.stringify({ scope, journeys } satisfies StoredMemory));
  } catch {
    // Quota or a disabled store. Losing the memory is acceptable.
  }
}

export interface JourneyMemoryApi {
  /** The remembered location for `journey`, or null. Callers MUST validate it
   *  against the live journey model before navigating to it. */
  remembered: (journey: JourneyId) => string | null;
  /** Record `path` as the current location of `journey`. */
  remember: (journey: JourneyId, path: string) => void;
  /** Forget everything for the current scope. */
  clear: () => void;
}

/**
 * Per-journey location memory scoped to one user and organization.
 *
 * @param userId Active user id from the shared session.
 * @param orgId Active organization id from the shared session.
 */
export function useJourneyMemory(
  userId: string | undefined,
  orgId: string | undefined,
): JourneyMemoryApi {
  const scope = useMemo(() => scopeKey(userId, orgId), [userId, orgId]);

  const remembered = useCallback(
    (journey: JourneyId): string | null => read(scope)[journey] ?? null,
    [scope],
  );

  const remember = useCallback(
    (journey: JourneyId, path: string) => {
      // Read-modify-write against the CURRENT scope only. If the stored value
      // belongs to another scope, `read` returns {} and this write replaces it.
      const next = { ...read(scope), [journey]: path };
      write(scope, next);
    },
    [scope],
  );

  const clear = useCallback(() => write(scope, {}), [scope]);

  return useMemo(() => ({ remembered, remember, clear }), [remembered, remember, clear]);
}
