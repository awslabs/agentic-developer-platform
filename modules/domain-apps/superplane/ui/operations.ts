/**
 * One operation identity per user intent — issue #5730.
 *
 * WHAT GOES WRONG WITHOUT THIS
 * ----------------------------
 * Creating a workspace provisions real infrastructure and spends real money. The
 * request is slow, so every ordinary accident of web and CLI use — a double
 * click, a reload while the spinner is up, a proxy that times out after the server
 * already accepted, a second terminal, a retry script — is an opportunity to build
 * a second workspace. AC-02 requires that "refresh, timeout and repeated submit
 * preserve one operation identity", and the only way to honour that is to decide
 * the identity *before* the first attempt and keep it across reloads.
 *
 * So an identity is minted once per intent, persisted with the payload it was
 * minted for, and reused by every subsequent attempt at that same intent.
 *
 * WHY THE PAYLOAD IS FINGERPRINTED ALONGSIDE IT
 * ---------------------------------------------
 * Reusing an identity is only safe while the intent is unchanged. If the user
 * edits the plan and submits again, that is a *different* intent, and sending it
 * under the old identity would ask the server to treat two different requests as
 * one — either silently returning the first workspace for the second request, or
 * conflicting. Both are wrong and neither is visible. So the payload is
 * fingerprinted: same fingerprint means retry and reuses the identity, different
 * fingerprint means new intent and is refused as a conflict for the caller to
 * resolve explicitly rather than resolved by a guess.
 *
 * WHY RECEIPTS ARE SCOPED TO THE ORGANIZATION
 * -------------------------------------------
 * A receipt names an operation inside one tenant. Replaying it after the user has
 * switched organizations would attach this tenant's screen to another tenant's
 * operation. Every stored record therefore carries its scope, and a read under a
 * different scope does not return it.
 *
 * Note what that already achieves, because it decides how much housekeeping is
 * safe: isolation between organizations is enforced by the *key and the embedded
 * scope check* on every read, not by deleting the other organization's records.
 * Deletion is therefore only ever housekeeping, and it must not be allowed to
 * destroy the one class of record that cannot be reconstructed — see
 * `pruneOtherScopes`.
 *
 * WHY A CLAIM RUNS UNDER A LOCK
 * -----------------------------
 * Minting is a read-modify-write over shared storage, and the events this design
 * exists to survive are exactly the ones that run it twice at once: a double
 * submit, a second tab, a restored session restoring several tabs together.
 * Unsynchronized, both readers see no receipt, both mint, and the two attempts
 * submit under different identities — so the server, which is deduplicating
 * faithfully, still builds two workspaces. `claimIdentityExclusive` therefore runs
 * the whole read-modify-write inside a named exclusive section so the second
 * caller reads the first caller's record and resumes it.
 *
 * WHAT IS STORED, AND WHAT IS NEVER STORED
 * ----------------------------------------
 * Only non-secret identifiers: the operation id, the idempotency key, the payload
 * fingerprint, the scope and timestamps. No credential value, no token, no raw
 * provider secret — those belong to ADP's vault and must not appear in browser
 * storage, which AC-03 requires and `receiptIsSecretFree` asserts.
 */

import type { OperationReceipt, OperationState } from './contract';

/** Storage key prefix. Namespaced so it cannot collide with ADP's own keys. */
const RECEIPT_PREFIX = 'adp.superplane.onboarding.receipt';

/**
 * The scope a receipt belongs to.
 *
 * Deployment is part of the scope as well as organization: the same organization
 * id in a different deployment is a different operation namespace, and a receipt
 * that crossed between them would point at an operation that does not exist there.
 */
export interface ReceiptScope {
  deploymentId: string;
  orgId: string;
  principalId?: string;
}

export interface StoredReceipt {
  idempotencyKey: string;
  /** Null until the server's first reply is seen. */
  operationId: string | null;
  fingerprint: string;
  scope: ReceiptScope;
  createdAt: string;
  /** Last state observed. `unknown` when a reply was lost. */
  state: OperationState;
  workspaceId: string | null;
  submissionStage?: 'draft' | 'approval' | 'submitted';
  approvalId?: string;
  accessRequestId?: string;
  accessOperationId?: string;
  retirementOperationId?: string;
}

/** The minimal storage surface used, so tests and the CLI can substitute one. */
export interface ReceiptStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  /** Enumerate keys, so a scope change can clear the other scope's records. */
  keys(): string[];
}

/** A `ReceiptStore` over the browser's `localStorage`. */
export function browserReceiptStore(storage: Storage): ReceiptStore {
  return {
    getItem: (key) => storage.getItem(key),
    setItem: (key, value) => storage.setItem(key, value),
    removeItem: (key) => storage.removeItem(key),
    keys: () => {
      const found: string[] = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key !== null) found.push(key);
      }
      return found;
    },
  };
}

/** An in-memory store. Used by tests and by any non-persistent caller. */
export function memoryReceiptStore(): ReceiptStore {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
    removeItem: (key) => map.delete(key),
    keys: () => [...map.keys()],
  };
}

function scopeKey(scope: ReceiptScope, intent: string): string {
  const principal = scope.principalId ? `principal:${encodeURIComponent(scope.principalId)}.` : '';
  return `${RECEIPT_PREFIX}.${scope.deploymentId}.${scope.orgId}.${principal}${intent}`;
}

/**
 * A stable fingerprint of the submission payload.
 *
 * Keys are sorted so that two objects differing only in property order — which
 * happens routinely when a form rebuilds its state — fingerprint identically and
 * are correctly recognised as the same intent rather than as a conflict.
 */
export function fingerprint(payload: unknown): string {
  const canonical = JSON.stringify(payload, (_key, value) => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const sorted: Record<string, unknown> = {};
      for (const key of Object.keys(value as Record<string, unknown>).sort()) {
        sorted[key] = (value as Record<string, unknown>)[key];
      }
      return sorted;
    }
    return value;
  });
  // A short non-cryptographic digest is sufficient: this detects *accidental*
  // payload drift between retries of the same user intent. It is not a security
  // boundary, and the server independently binds the submission to the plan
  // revision it approved.
  let hash = 0x811c9dc5;
  for (let index = 0; index < canonical.length; index += 1) {
    hash ^= canonical.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

export type ClaimOutcome =
  | { kind: 'new'; receipt: StoredReceipt }
  | { kind: 'resume'; receipt: StoredReceipt }
  | { kind: 'conflict'; existing: StoredReceipt; detail: string };

/**
 * Obtain the identity to submit under.
 *
 * `new` mints one, `resume` returns the identity an earlier attempt at this same
 * intent already used, and `conflict` reports that a *different* payload is
 * already in flight under this intent. A conflict is surfaced rather than resolved
 * because only the user can say whether they meant to replace the earlier request
 * — and the earlier request may already have built something.
 *
 * `mintKey` is injected rather than calling `crypto.randomUUID()` directly so a
 * test can assert identity reuse deterministically.
 */
export function claimIdentity(
  store: ReceiptStore,
  scope: ReceiptScope,
  intent: string,
  payload: unknown,
  mintKey: () => string,
  nowIso: string,
): ClaimOutcome {
  const key = scopeKey(scope, intent);
  const raw = store.getItem(key);
  const print = fingerprint(payload);

  if (raw) {
    let existing: StoredReceipt | null = null;
    try {
      existing = JSON.parse(raw) as StoredReceipt;
    } catch {
      // Unparseable state is treated as absent rather than as a hard failure:
      // refusing to proceed would leave the user permanently unable to onboard
      // with no way to clear it from the UI.
      existing = null;
    }
    if (existing && sameScope(existing.scope, scope)) {
      if (existing.fingerprint === print) {
        return { kind: 'resume', receipt: existing };
      }
      if (isTerminal(existing.state)) {
        // The previous intent finished. A changed payload is now a genuinely new
        // request, so a fresh identity is correct and no conflict exists.
        const receipt = blank(print, scope, mintKey(), nowIso);
        store.setItem(key, JSON.stringify(receipt));
        return { kind: 'new', receipt };
      }
      return {
        kind: 'conflict',
        existing,
        detail:
          'A different submission is already in progress for this onboarding ' +
          'step. Resolve or wait for it before submitting changed inputs, so ' +
          'two workspaces are not created.',
      };
    }
  }

  const receipt = blank(print, scope, mintKey(), nowIso);
  store.setItem(key, JSON.stringify(receipt));
  return { kind: 'new', receipt };
}

function blank(
  print: string,
  scope: ReceiptScope,
  idempotencyKey: string,
  nowIso: string,
): StoredReceipt {
  return {
    idempotencyKey,
    operationId: null,
    fingerprint: print,
    scope,
    createdAt: nowIso,
    state: 'accepted',
    workspaceId: null,
  };
}

function sameScope(current: ReceiptScope, expected: ReceiptScope): boolean {
  return current.deploymentId === expected.deploymentId && current.orgId === expected.orgId &&
    current.principalId === expected.principalId;
}

export function isTerminal(state: OperationState): boolean {
  return state === 'succeeded' || state === 'failed' || state === 'cancelled';
}

/**
 * Record what the server said.
 *
 * `unknown` is recorded as `unknown` and never rewritten to `failed`. Collapsing
 * the two is the single most damaging simplification available here: reporting a
 * lost reply as a failure invites a resubmission, and the operation it would
 * duplicate may have succeeded.
 */
export function recordObservation(
  store: ReceiptStore,
  scope: ReceiptScope,
  intent: string,
  observation: Pick<OperationReceipt, 'operationId' | 'state' | 'workspaceId'> & { idempotencyKey?: string },
): StoredReceipt | null {
  const key = scopeKey(scope, intent);
  const raw = store.getItem(key);
  if (!raw) return null;
  let receipt: StoredReceipt;
  try {
    receipt = JSON.parse(raw) as StoredReceipt;
  } catch {
    return null;
  }
  if (!sameScope(receipt.scope, scope)) return null;
  if (observation.idempotencyKey && observation.idempotencyKey !== receipt.idempotencyKey) return null;
  if (isTerminal(receipt.state) && !isTerminal(observation.state)) return receipt;
  const updated: StoredReceipt = {
    ...receipt,
    operationId: observation.operationId ?? receipt.operationId,
    state: observation.state,
    workspaceId: observation.workspaceId ?? receipt.workspaceId,
  };
  store.setItem(key, JSON.stringify(updated));
  return updated;
}

/** The receipt for this intent, or null. Never returns another scope's record. */
export function readReceipt(
  store: ReceiptStore,
  scope: ReceiptScope,
  intent: string,
): StoredReceipt | null {
  const raw = store.getItem(scopeKey(scope, intent));
  if (!raw) return null;
  try {
    const receipt = JSON.parse(raw) as StoredReceipt;
    return sameScope(receipt.scope, scope) ? receipt : null;
  } catch {
    return null;
  }
}

/** Recover submitted continuations even after their proposal leaves the current phase. */
export function receiptsForIntentPrefix(store: ReceiptStore, scope: ReceiptScope, prefix: string) {
  const namespace = scopeKey(scope, '');
  return store.keys().filter((key) => key.startsWith(`${namespace}${prefix}`)).flatMap((key) => {
    const intent = key.slice(namespace.length);
    const receipt = readReceipt(store, scope, intent);
    return receipt ? [{ intent, receipt }] : [];
  });
}

/** What a prune did, so a caller can assert it or log it rather than assume it. */
export interface PruneResult {
  /** Keys removed: settled records only. */
  removed: string[];
  /** Keys deliberately kept because their operation is still unresolved. */
  retained: string[];
}

/**
 * Tidy away *settled* receipts from scopes other than `keep`.
 *
 * WHAT THIS USED TO DO, AND WHY IT WAS DATA LOSS
 * ---------------------------------------------
 * It removed every record outside the kept scope unconditionally. That destroyed
 * precisely the records that cannot be rebuilt. Consider the sequence the design
 * exists for: in organization A a create is submitted, the reply is lost, and the
 * receipt is left `unknown` — a workspace may exist and may be billing. The user
 * switches to B to look at something, then back to A. The switch to B deleted A's
 * `unknown` receipt, so on return A shows a blank form, the warning is gone, the
 * preserved operation identity is gone, and the obvious next action — fill it in
 * again and submit — mints a *new* identity and builds the second workspace. The
 * one guarantee AC-02 asks for is broken by the housekeeping, not by the server.
 *
 * WHY DELETING IS NOT WHAT KEEPS TENANTS APART
 * -------------------------------------------
 * The isolation requirement is that this tenant's screen never attaches to another
 * tenant's operation, and that is already enforced on every read: the key carries
 * the scope and `readReceipt` additionally checks the scope embedded in the record.
 * A retained A record is unreachable while B is selected whether or not it is on
 * disk. So deletion buys no isolation and costs the recovery path.
 *
 * WHAT IS THEREFORE PRUNED
 * ------------------------
 * Only records that are settled — `succeeded` or `failed` — plus records too
 * corrupt to interpret, which cannot inform a recovery either. Anything still
 * unresolved (`unknown` after a lost reply, or `accepted`/`running` mid-flight) is
 * kept, and kept under its own scope, where only that scope can read it.
 */
export function pruneOtherScopes(store: ReceiptStore, keep: ReceiptScope): PruneResult {
  const prefix = `${RECEIPT_PREFIX}.${keep.deploymentId}.${keep.orgId}.`;
  const removed: string[] = [];
  const retained: string[] = [];
  for (const key of store.keys()) {
    if (!key.startsWith(RECEIPT_PREFIX) || key.startsWith(prefix)) continue;
    const raw = store.getItem(key);
    let record: StoredReceipt | null = null;
    try {
      record = raw === null ? null : (JSON.parse(raw) as StoredReceipt);
    } catch {
      record = null;
    }
    // An uninterpretable record is removed: it can neither be resumed nor
    // reported, so keeping it protects nothing and only accumulates.
    if (record && !isTerminal(record.state)) {
      retained.push(key);
      continue;
    }
    store.removeItem(key);
    removed.push(key);
  }
  return { removed, retained };
}

/**
 * Run `body` with nobody else inside the same named section.
 *
 * WHERE THE RACE ACTUALLY IS
 * --------------------------
 * Not inside one document. `claimIdentity` is synchronous, so two submits in the
 * same page cannot interleave within it — the second necessarily reads what the
 * first wrote. The race is between *documents*: two tabs of the same profile run
 * on separate event loops over one shared `localStorage`, so tab A's read can
 * precede tab B's write even though each is synchronous in its own loop. Both then
 * mint, and two identities defeat deduplication however correctly the server
 * implements it.
 *
 * `navigator.locks` is the browser's own cross-document mutex and is the only
 * mechanism here that closes that. Where it is absent the fallback serializes
 * within this document only — which is not a fix for a cross-tab race, and is not
 * dressed up as one: {@link exclusiveSectionIsCrossTab} reports which of the two a
 * given browser provides, so a caller can state the limit rather than assume the
 * guarantee. `navigator.locks` is available in every browser this product
 * supports; the fallback exists for non-browser hosts and for tests.
 */
export type ExclusiveSection = <T>(name: string, body: () => T | Promise<T>) => Promise<T>;

interface LockManagerLike {
  request(name: string, callback: () => Promise<unknown>): Promise<unknown>;
}

/** The browser's lock manager, or null where it is not implemented. */
function lockManager(): LockManagerLike | null {
  if (typeof navigator === 'undefined') return null;
  const locks = (navigator as unknown as { locks?: LockManagerLike }).locks;
  return locks && typeof locks.request === 'function' ? locks : null;
}

/** Whether exclusion actually spans tabs here, or only this document. */
export function exclusiveSectionIsCrossTab(): boolean {
  return lockManager() !== null;
}

/** In-document serialization: the per-name tail of the queue. */
const localQueue = new Map<string, Promise<unknown>>();

/** The default section runner: a browser lock when there is one, a queue when not. */
export const browserExclusive: ExclusiveSection = <T,>(
  name: string,
  body: () => T | Promise<T>,
): Promise<T> => {
  const locks = lockManager();
  if (locks) {
    return locks.request(name, async () => await body()) as Promise<T>;
  }
  const previous = localQueue.get(name) ?? Promise.resolve();
  // `.catch` so one failed body does not wedge every later claim on this name.
  const next = previous.then(
    () => body(),
    () => body(),
  );
  localQueue.set(
    name,
    next.catch(() => undefined),
  );
  return next;
};

/**
 * Claim an identity with the read-modify-write held exclusive.
 *
 * WHY THE PLAIN FUNCTION IS NOT ENOUGH
 * -----------------------------------
 * `claimIdentity` reads, decides and writes. Two callers that interleave inside
 * that window both read "no receipt", both mint, and both submit — under different
 * identities, so a server deduplicating perfectly still builds two workspaces. The
 * window is not theoretical: a double-clicked button, two tabs restored together,
 * and a retry script are the ordinary ways to hit it.
 *
 * WHAT THIS DOES NOT DO
 * ---------------------
 * It adds no second-guessing after the claim. An earlier draft re-read the store
 * afterwards so a write-race loser could adopt the stored key; mutation testing
 * showed the branch was unreachable — under a real lock the re-read can only
 * confirm, and without one the losing tab has already overwritten the record it
 * would need to find. Unreachable code that looks like a safety net is worse than
 * none, because it invites the reader to believe the degraded path is covered. The
 * lock is the mechanism; `exclusiveSectionIsCrossTab` is how a caller learns
 * whether it has one.
 *
 * The section name is per scope AND per intent, so two organizations claiming at
 * once do not serialize against each other.
 */
export async function claimIdentityExclusive(
  store: ReceiptStore,
  scope: ReceiptScope,
  intent: string,
  payload: unknown,
  mintKey: () => string,
  nowIso: string,
  section: ExclusiveSection = browserExclusive,
): Promise<ClaimOutcome> {
  const key = scopeKey(scope, intent);
  return section(`${key}.claim`, () =>
    claimIdentity(store, scope, intent, payload, mintKey, nowIso),
  );
}

/** Persist a draft identity before preview. Only an unsubmitted draft may be replaced. */
export async function claimPreviewIdentity(
  store: ReceiptStore, scope: ReceiptScope, intent: string, payload: unknown,
  mintKey: () => string, nowIso: string, section: ExclusiveSection = browserExclusive,
): Promise<ClaimOutcome> {
  return section(`${scopeKey(scope, intent)}.claim`, () => {
    const existing = readReceipt(store, scope, intent);
    if (existing?.submissionStage === 'draft' && existing.fingerprint !== fingerprint(payload)) {
      const receipt = { ...blank(fingerprint(payload), scope, mintKey(), nowIso), submissionStage: 'draft' as const };
      store.setItem(scopeKey(scope, intent), JSON.stringify(receipt));
      return { kind: 'new' as const, receipt };
    }
    const claim = claimIdentity(store, scope, intent, payload, mintKey, nowIso);
    if (claim.kind === 'new') {
      claim.receipt.submissionStage = 'draft';
      store.setItem(scopeKey(scope, intent), JSON.stringify(claim.receipt));
    }
    return claim;
  });
}

export async function markSubmissionStage(
  store: ReceiptStore, scope: ReceiptScope, intent: string, requestId: string,
  stage: 'approval' | 'submitted', approvalId?: string, section: ExclusiveSection = browserExclusive,
): Promise<StoredReceipt | null> {
  return section(`${scopeKey(scope, intent)}.claim`, () => {
    const receipt = readReceipt(store, scope, intent);
    if (!receipt || receipt.idempotencyKey !== requestId) return null;
    if (receipt.submissionStage === 'submitted' && stage === 'approval') return receipt;
    const updated = { ...receipt, submissionStage: stage, ...(approvalId ? { approvalId } : {}) };
    store.setItem(scopeKey(scope, intent), JSON.stringify(updated));
    return updated;
  });
}

export async function recordRetirementLineage(
  store: ReceiptStore, scope: ReceiptScope, intent: string, requestId: string, workspaceId: string,
  identities: Pick<StoredReceipt, 'accessRequestId' | 'accessOperationId' | 'retirementOperationId'>,
  section: ExclusiveSection = browserExclusive,
): Promise<StoredReceipt | null> {
  return section(`${scopeKey(scope, intent)}.claim`, () => {
    const receipt = readReceipt(store, scope, intent);
    if (!receipt || receipt.idempotencyKey !== requestId ||
        (receipt.workspaceId !== null && receipt.workspaceId !== workspaceId)) return null;
    for (const field of ['accessRequestId', 'accessOperationId', 'retirementOperationId'] as const) {
      const identity = identities[field];
      if (identity !== undefined && (!identity || (receipt[field] && receipt[field] !== identity))) return null;
    }
    const updated: StoredReceipt = { ...receipt, workspaceId, ...identities };
    if (!receiptIsSecretFree(updated)) return null;
    store.setItem(scopeKey(scope, intent), JSON.stringify(updated));
    return updated;
  });
}

/** Observation writes and identity claims use the same cross-tab lock. */
export async function recordObservationExclusive(
  store: ReceiptStore, scope: ReceiptScope, intent: string,
  observation: Pick<OperationReceipt, 'operationId' | 'state' | 'workspaceId' | 'idempotencyKey'>,
  section: ExclusiveSection = browserExclusive,
): Promise<StoredReceipt | null> {
  return section(`${scopeKey(scope, intent)}.claim`, () => recordObservation(store, scope, intent, observation));
}

/**
 * The operation state a create/adopt reply establishes — rarely `succeeded`.
 *
 * WHY A READABLE REPLY IS NOT A FINISHED OPERATION
 * -----------------------------------------------
 * `POST /workspaces` answers 201 with `status: "Provisioning"` and then builds the
 * cluster asynchronously (`routers/workspaces.py`). Recording that as `succeeded`
 * — which this flow used to do for any 2xx at all — asserts a terminal outcome the
 * server never claimed, and terminal is load-bearing twice over: `claimIdentity`
 * lets a *changed* payload mint a fresh identity once the stored receipt is
 * terminal, and the user reads the state as "ready". So the wrong terminal state
 * both permits a duplicate submission and reports infrastructure as usable while
 * provisioning may still fail.
 *
 * The mapping is deliberately narrow, case-folded, and non-terminal by default.
 * Anything unrecognised is a wait, because the cost of waiting is one poll while
 * the cost of a wrong terminal state is a duplicate or a false assurance.
 *
 * The domain's own vocabulary is not internally consistent — the router writes
 * `Provisioning`, `Teardown` and `Failed`, the model defines `pending`,
 * `bootstrapping`, `active`, `reconciling` and `drift_detected`, and the proxy
 * admits work only on `Active`. That is mirrored here as observed rather than
 * tidied into a uniformity the server does not have; `adp-superplane-onboarding.py`
 * maps the same strings the same way so the two clients cannot disagree about
 * whether an operation is over.
 */
export function stateFromWorkspaceStatus(status: string | null | undefined): OperationState {
  if (typeof status !== 'string' || status.trim() === '') return 'accepted';
  const folded = status.trim().toLowerCase();
  if (folded === 'failed') return 'failed';
  if (folded === 'active' || folded === 'ready' || folded === 'healthy') return 'succeeded';
  return 'running';
}

/**
 * Patterns that must never appear in a persisted receipt.
 *
 * Asserted by a test rather than trusted, because AC-03 requires secrets to be
 * absent from browser persistence and a comment promising it is not evidence.
 */
const SECRET_SHAPES: readonly RegExp[] = [
  /AKIA[0-9A-Z]{16}/,
  /ASIA[0-9A-Z]{16}/,
  /\bBEGIN [A-Z ]*PRIVATE KEY\b/,
  /\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./, // a JWT
  /\bsecret\b/i,
  /\bpassword\b/i,
  /\bapi[_-]?key\b/i,
];

/** True when the serialized receipt carries no secret-shaped material. */
export function receiptIsSecretFree(receipt: StoredReceipt): boolean {
  const serialized = JSON.stringify(receipt);
  return !SECRET_SHAPES.some((shape) => shape.test(serialized));
}
