/**
 * One operation identity survives every ordinary accident — #5730 AC-02/AC-03.
 *
 * AC-02 requires that "refresh, timeout and repeated submit preserve one operation
 * identity". Each of those three words is a separate test below, because they fail
 * differently: a repeated submit is two calls in one page, a refresh is two calls
 * across two pages, and a timeout is one call whose answer never arrives. All three
 * must end with one workspace.
 *
 * AC-03 requires that secrets are absent from browser persistence. That is asserted
 * against a populated receipt rather than argued from the type definition.
 */

import { claimPreviewIdentity, markSubmissionStage } from '@superplane-ui/operations';

import { describe, expect, it } from 'vitest';

import {
  browserReceiptStore,
  claimIdentity,
  claimIdentityExclusive,
  exclusiveSectionIsCrossTab,
  fingerprint,
  isTerminal,
  memoryReceiptStore,
  pruneOtherScopes,
  readReceipt,
  receiptIsSecretFree,
  recordObservation,
  type ExclusiveSection,
  type ReceiptScope,
  type ReceiptStore,
} from '@superplane-ui/operations';

const SCOPE: ReceiptScope = { deploymentId: 'dev', orgId: 'org-1' };
const OTHER_ORG: ReceiptScope = { deploymentId: 'dev', orgId: 'org-2' };
const OTHER_DEPLOYMENT: ReceiptScope = { deploymentId: 'prod', orgId: 'org-1' };
const INTENT = 'create-workspace';
const NOW_ISO = '2026-09-23T12:00:00.000Z';

const PAYLOAD = { name: 'research', isolation_mode: 'dedicated', account: '111122223333' };

/** A counter-based key minter, so identity reuse is observable. */
function minter(): () => string {
  let count = 0;
  return () => {
    count += 1;
    return `key-${count}`;
  };
}

describe('repeated submit', () => {
  it('reuses one identity when the same payload is submitted twice', () => {
    // The double-click case. Two submissions, one operation identity, so the
    // server can recognise the second as a retry of the first.
    const store = memoryReceiptStore();
    const mint = minter();

    const first = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    const second = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);

    expect(first.kind).toBe('new');
    expect(second.kind).toBe('resume');
    expect(second.receipt.idempotencyKey).toBe(first.receipt.idempotencyKey);
    expect(second.receipt.idempotencyKey).toBe('key-1');
  });

  it('reuses the identity for a concurrent second submit before any reply', () => {
    // The two-terminals / two-tabs case. Neither attempt has seen a reply, so
    // there is no state to distinguish them — and they must still share identity.
    const store = memoryReceiptStore();
    const mint = minter();

    const a = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    const b = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    const c = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);

    expect(new Set([a, b, c].map((o) => o.receipt.idempotencyKey)).size).toBe(1);
  });
});

describe('refresh', () => {
  it('recovers the in-flight identity from persistence after a reload', () => {
    // A reload destroys all in-memory state. The identity has to come back from
    // storage or the next submit builds a second workspace.
    const storage = new Map<string, string>();
    const store = () =>
      browserReceiptStore({
        getItem: (k: string) => storage.get(k) ?? null,
        setItem: (k: string, v: string) => void storage.set(k, v),
        removeItem: (k: string) => void storage.delete(k),
        key: (i: number) => [...storage.keys()][i] ?? null,
        get length() {
          return storage.size;
        },
        clear: () => storage.clear(),
      } as unknown as Storage);

    const before = claimIdentity(store(), SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);
    // Simulate the reload: brand new store object over the same backing storage.
    const after = readReceipt(store(), SCOPE, INTENT);

    expect(after).not.toBeNull();
    expect(after?.idempotencyKey).toBe(before.receipt.idempotencyKey);
  });

  it('resumes rather than mints when the page reloads mid-submission', () => {
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);

    const afterReload = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    expect(afterReload.kind).toBe('resume');
    expect(afterReload.receipt.idempotencyKey).toBe('key-1');
  });
});

describe('timeout and lost replies', () => {
  it('keeps an unknown outcome unknown instead of calling it a failure', () => {
    // The most consequential rule in the module. A lost reply means the operation
    // may have succeeded. Recording it as `failed` invites a resubmission that
    // duplicates real infrastructure and real spend.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);

    const updated = recordObservation(store, SCOPE, INTENT, {
      operationId: 'op-1',
      state: 'unknown',
      workspaceId: null,
    });

    expect(updated?.state).toBe('unknown');
    expect(isTerminal('unknown')).toBe(false);
  });

  it('retries an unknown outcome under the original identity', () => {
    // Because `unknown` is not terminal, the retry resumes rather than minting —
    // so the server sees the same identity and can answer with the first result.
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    recordObservation(store, SCOPE, INTENT, { operationId: 'op-1', state: 'unknown', workspaceId: null });

    const retry = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    expect(retry.kind).toBe('resume');
    expect(retry.receipt.idempotencyKey).toBe('key-1');
  });

  it('retains the operation id learned before the reply was lost', () => {
    // If the server named the operation before the connection dropped, that name
    // is how the outcome gets reconciled later. Losing it loses the trail.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);
    recordObservation(store, SCOPE, INTENT, { operationId: 'op-1', state: 'running', workspaceId: null });
    const later = recordObservation(store, SCOPE, INTENT, {
      operationId: null as unknown as string,
      state: 'unknown',
      workspaceId: null,
    });

    expect(later?.operationId).toBe('op-1');
  });
});

describe('changed payload', () => {
  it('refuses to reuse an in-flight identity for different inputs', () => {
    // Sending changed inputs under the old identity asks the server to treat two
    // different requests as one: it either returns the first workspace for the
    // second request or conflicts. Neither is visible to the user, so the client
    // stops and says so.
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);

    const changed = claimIdentity(
      store,
      SCOPE,
      INTENT,
      { ...PAYLOAD, name: 'different' },
      mint,
      NOW_ISO,
    );

    expect(changed.kind).toBe('conflict');
    if (changed.kind === 'conflict') {
      expect(changed.existing.idempotencyKey).toBe('key-1');
      expect(changed.detail).toMatch(/already in progress/i);
    }
  });

  it('allows a new identity for changed inputs once the previous run finished', () => {
    // A completed operation is not in flight, so changed inputs are a genuinely
    // new request. Blocking here would leave the user unable to create a second
    // workspace after their first succeeded.
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    recordObservation(store, SCOPE, INTENT, { operationId: 'op-1', state: 'succeeded', workspaceId: 'ws-1' });

    const next = claimIdentity(store, SCOPE, INTENT, { ...PAYLOAD, name: 'second' }, mint, NOW_ISO);
    expect(next.kind).toBe('new');
    expect(next.receipt.idempotencyKey).toBe('key-2');
  });

  it('allows a fresh attempt with changed inputs after a failure', () => {
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    recordObservation(store, SCOPE, INTENT, { operationId: 'op-1', state: 'failed', workspaceId: null });

    const fixed = claimIdentity(store, SCOPE, INTENT, { ...PAYLOAD, account: '999988887777' }, mint, NOW_ISO);
    expect(fixed.kind).toBe('new');
  });
});

describe('payload fingerprinting', () => {
  it('ignores property order, which a rebuilt form changes routinely', () => {
    // Without key sorting, a form that re-renders its state object in a different
    // order would look like changed inputs and produce a spurious conflict.
    expect(fingerprint({ a: 1, b: 2 })).toBe(fingerprint({ b: 2, a: 1 }));
  });

  it('ignores nested property order too', () => {
    expect(fingerprint({ outer: { a: 1, b: 2 } })).toBe(fingerprint({ outer: { b: 2, a: 1 } }));
  });

  it('does not ignore array order, which is meaningful', () => {
    expect(fingerprint({ xs: [1, 2] })).not.toBe(fingerprint({ xs: [2, 1] }));
  });

  it('distinguishes a changed value', () => {
    expect(fingerprint(PAYLOAD)).not.toBe(fingerprint({ ...PAYLOAD, name: 'other' }));
  });

  it('distinguishes an absent field from an explicitly null one', () => {
    // `{account: null}` is a request to clear it; omitting it leaves it alone.
    expect(fingerprint({ name: 'x' })).not.toBe(fingerprint({ name: 'x', account: null }));
  });
});

describe('organization and deployment scoping', () => {
  it('does not return another organization receipt', () => {
    // AC-03: cross-org access is denied. Returning this receipt would attach the
    // current tenant's screen to another tenant's operation.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);

    expect(readReceipt(store, OTHER_ORG, INTENT)).toBeNull();
  });

  it('does not return a receipt from another deployment', () => {
    // The same org id in a different deployment is a different operation
    // namespace; the operation simply does not exist there.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);

    expect(readReceipt(store, OTHER_DEPLOYMENT, INTENT)).toBeNull();
  });

  it('mints a separate identity per organization for the same intent', () => {
    // Two orgs each creating a workspace are two operations, not a retry.
    const store = memoryReceiptStore();
    const mint = minter();
    const first = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    const second = claimIdentity(store, OTHER_ORG, INTENT, PAYLOAD, mint, NOW_ISO);

    expect(second.kind).toBe('new');
    expect(second.receipt.idempotencyKey).not.toBe(first.receipt.idempotencyKey);
  });

  it('tidies away a settled receipt from a scope the user has left', () => {
    // Housekeeping is allowed on records that are finished: nothing can be
    // recovered from them, so keeping them only accumulates.
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    claimIdentity(store, OTHER_ORG, INTENT, PAYLOAD, mint, NOW_ISO);
    recordObservation(store, OTHER_ORG, INTENT, {
      operationId: 'op-2',
      state: 'succeeded',
      workspaceId: 'ws-2',
    });

    const result = pruneOtherScopes(store, SCOPE);

    expect(readReceipt(store, SCOPE, INTENT)).not.toBeNull();
    expect(readReceipt(store, OTHER_ORG, INTENT)).toBeNull();
    expect(result.removed).toHaveLength(1);
    expect(result.retained).toEqual([]);
  });

  it('keeps an unresolved receipt from another organization instead of deleting it', () => {
    // The data-loss defect, stated as its consequence. A lost reply in org B
    // leaves an `unknown` receipt that is the ONLY record of an operation that may
    // have built and be billing. Selecting org A must not destroy it: isolation is
    // already enforced by the scoped key and by readReceipt's own scope check, so
    // deletion buys nothing and costs the recovery.
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, OTHER_ORG, INTENT, PAYLOAD, mint, NOW_ISO);
    recordObservation(store, OTHER_ORG, INTENT, {
      operationId: 'op-lost',
      state: 'unknown',
      workspaceId: null,
    });

    const result = pruneOtherScopes(store, SCOPE);

    expect(readReceipt(store, OTHER_ORG, INTENT)).not.toBeNull();
    expect(result.retained).toHaveLength(1);
    expect(result.removed).toEqual([]);
    // Retained does NOT mean readable from here. The point of keeping it is
    // recovery in its own organization, not visibility in this one.
    expect(readReceipt(store, SCOPE, INTENT)).toBeNull();
  });

  it('keeps an in-flight receipt from another organization', () => {
    // `accepted` is the state a submission sits in between the request leaving and
    // a reply landing. Pruning it mid-flight loses the identity the in-flight
    // request was sent under, so its own retry would mint a second one.
    const store = memoryReceiptStore();
    claimIdentity(store, OTHER_ORG, INTENT, PAYLOAD, minter(), NOW_ISO);

    const result = pruneOtherScopes(store, SCOPE);

    expect(readReceipt(store, OTHER_ORG, INTENT)).not.toBeNull();
    expect(result.retained).toHaveLength(1);
  });

  it('returns an unresolved receipt after a switch away and back', () => {
    // AC-02's stated journey: A submits, the reply is lost, the user visits B and
    // returns to A. The prune runs on each switch. A's receipt — and its identity
    // — must still be there, because the screen it drives is what stops the user
    // resubmitting and building a second workspace.
    const store = memoryReceiptStore();
    const mint = minter();
    const claim = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    recordObservation(store, SCOPE, INTENT, {
      operationId: 'op-lost',
      state: 'unknown',
      workspaceId: null,
    });

    pruneOtherScopes(store, OTHER_ORG); // switch A -> B
    pruneOtherScopes(store, SCOPE); //     switch B -> A

    const recovered = readReceipt(store, SCOPE, INTENT);
    expect(recovered?.state).toBe('unknown');
    expect(recovered?.idempotencyKey).toBe(claim.receipt.idempotencyKey);
    // And a resubmission of the same inputs resumes rather than mints.
    const again = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    expect(again.kind).toBe('resume');
    expect(again.receipt.idempotencyKey).toBe(claim.receipt.idempotencyKey);
  });

  it('removes an uninterpretable record rather than keeping it forever', () => {
    // Corrupt state cannot be resumed or reported, so retaining it protects
    // nothing. Distinguished from the unresolved case on purpose: "cannot be read"
    // and "not yet decided" are different, and only one of them is recoverable.
    const store = memoryReceiptStore();
    store.setItem('adp.superplane.onboarding.receipt.dev.org-2.create-workspace', '{not json');

    const result = pruneOtherScopes(store, SCOPE);

    expect(result.removed).toHaveLength(1);
    expect(result.retained).toEqual([]);
  });

  it('leaves unrelated application storage alone when pruning scopes', () => {
    // Pruning must not reach outside this feature's namespace — evicting the
    // session token would log the user out on an org switch.
    const store = memoryReceiptStore();
    store.setItem('cognito_access_token', 'unrelated');
    claimIdentity(store, OTHER_ORG, INTENT, PAYLOAD, minter(), NOW_ISO);
    recordObservation(store, OTHER_ORG, INTENT, {
      operationId: 'op-2',
      state: 'failed',
      workspaceId: null,
    });

    pruneOtherScopes(store, SCOPE);

    expect(store.getItem('cognito_access_token')).toBe('unrelated');
  });

  it('refuses a stored record whose embedded scope disagrees with its key', () => {
    // Defence against a tampered or migrated store: the scope inside the record
    // is checked, not just the key it was filed under.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);
    const key = [...store.keys()][0];
    const record = JSON.parse(store.getItem(key)!);
    record.scope = OTHER_ORG;
    store.setItem(key, JSON.stringify(record));

    expect(readReceipt(store, SCOPE, INTENT)).toBeNull();
  });
});

describe('secret absence in persistence', () => {
  it('stores only non-secret identifiers', () => {
    // AC-03: secrets absent from browser persistence. Asserted against the real
    // serialized record, not argued from the type.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);
    recordObservation(store, SCOPE, INTENT, { operationId: 'op-1', state: 'running', workspaceId: 'ws-1' });

    const receipt = readReceipt(store, SCOPE, INTENT)!;
    expect(receiptIsSecretFree(receipt)).toBe(true);
    expect(Object.keys(receipt).sort()).toEqual([
      'createdAt',
      'fingerprint',
      'idempotencyKey',
      'operationId',
      'scope',
      'state',
      'workspaceId',
    ]);
  });

  it('does not persist the submitted payload, only its fingerprint', () => {
    // The payload can name a cloud account and could later grow a credential
    // field. Keeping only the digest means a future field cannot leak by default.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);

    const serialized = store.getItem([...store.keys()][0])!;
    expect(serialized).not.toContain('111122223333');
    expect(serialized).not.toContain('research');
  });

  it('detects secret-shaped material if a future change introduces it', () => {
    // Proves the guard can fail — a check that cannot detect anything is not a
    // check. Built by hand rather than by storing a secret through the real path.
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);
    const receipt = readReceipt(store, SCOPE, INTENT)!;

    expect(
      receiptIsSecretFree({
        ...receipt,
        workspaceId: 'AKIAIOSFODNN7EXAMPLE',
      }),
    ).toBe(false);
  });
});

describe('corrupt persistence', () => {
  it('treats unparseable state as absent rather than blocking onboarding', () => {
    // A hard failure here would leave the user permanently unable to onboard with
    // no way to clear it from the UI. Minting a new identity is the safe recovery:
    // there is no readable identity to duplicate.
    const store = memoryReceiptStore();
    const mint = minter();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    store.setItem([...store.keys()][0], '{not json');

    const outcome = claimIdentity(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO);
    expect(outcome.kind).toBe('new');
    expect(readReceipt(store, SCOPE, INTENT)).not.toBeNull();
  });

  it('returns null for a corrupt record rather than throwing', () => {
    const store = memoryReceiptStore();
    claimIdentity(store, SCOPE, INTENT, PAYLOAD, minter(), NOW_ISO);
    store.setItem([...store.keys()][0], 'not json at all');

    expect(() => readReceipt(store, SCOPE, INTENT)).not.toThrow();
    expect(readReceipt(store, SCOPE, INTENT)).toBeNull();
  });

  it('does not record an observation for an intent that was never claimed', () => {
    // A late reply for a receipt that has been cleared (org switch) must be
    // discarded, not resurrected as a new record in the current scope.
    const store = memoryReceiptStore();
    expect(recordObservation(store, SCOPE, INTENT, { operationId: 'op-1', state: 'succeeded', workspaceId: 'ws-1' })).toBeNull();
    expect(readReceipt(store, SCOPE, INTENT)).toBeNull();
  });
});

/**
 * Two claims at once.
 *
 * The damaging interleaving is not exotic: both callers read "no receipt" before
 * either writes, so both mint, and the two submissions carry different identities.
 * A server deduplicating perfectly still builds two workspaces, because it was
 * never told the two requests were one. These tests drive the interleaving
 * deterministically rather than starting two promises and hoping.
 */
describe('concurrent claims', () => {
  /** The claimed key, or null for a conflict, which has no receipt. */
  function keyOf(outcome: Awaited<ReturnType<typeof claimIdentityExclusive>>): string | null {
    return outcome.kind === 'conflict' ? null : outcome.receipt.idempotencyKey;
  }

  /** A section runner that serializes by name, like the browser's lock manager. */
  function serializing(): ExclusiveSection {
    const tails = new Map<string, Promise<unknown>>();
    return <T,>(name: string, body: () => T | Promise<T>): Promise<T> => {
      const previous = tails.get(name) ?? Promise.resolve();
      const next = previous.then(() => body());
      tails.set(
        name,
        next.catch(() => undefined),
      );
      return next;
    };
  }

  it('mints one identity when two claims are interleaved under exclusion', async () => {
    // Both callers start before either finishes. Exclusion makes the second read
    // the first's record, so one identity is submitted under.
    const store = memoryReceiptStore();
    const mint = minter();
    const section = serializing();

    const [first, second] = await Promise.all([
      claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, section),
      claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, section),
    ]);

    expect(keyOf(first)).toBe('key-1');
    expect(keyOf(second)).toBe('key-1');
    expect(second.kind).toBe('resume');
  });

  /**
   * Two documents over one `localStorage`.
   *
   * Modelling this needs care, because the race cannot be produced by suspending
   * inside a claim: `claimIdentity` is synchronous, so within one event loop the
   * second call necessarily reads what the first wrote. What actually differs
   * between two tabs is WHEN each document last read the shared storage. So each
   * document here reads through a snapshot, and the snapshot is refreshed at the
   * moment that document enters its section — which is exactly where a real tab
   * re-reads `localStorage`. Writes go straight through to the shared backing,
   * because a real write is immediately visible to whoever reads next.
   *
   * The consequence is the point of the whole mechanism: under exclusion the
   * documents enter one at a time, so the second's refresh happens after the
   * first's write and it sees it. Without exclusion both refresh before either
   * writes, and both see nothing.
   */
  function twoDocuments() {
    const backing = new Map<string, string>();
    const documents: Array<{ snapshot: Map<string, string>; store: ReceiptStore }> = [];

    function document(): ReceiptStore {
      const entry = { snapshot: new Map<string, string>(), store: null as unknown as ReceiptStore };
      entry.store = {
        getItem: (key) => entry.snapshot.get(key) ?? null,
        setItem: (key, value) => {
          backing.set(key, value);
          entry.snapshot.set(key, value);
        },
        removeItem: (key) => {
          backing.delete(key);
          entry.snapshot.delete(key);
        },
        keys: () => [...entry.snapshot.keys()],
      };
      documents.push(entry);
      return entry.store;
    }

    /**
     * A section runner that refreshes the calling document's view on entry, with a
     * turn of the event loop between the refresh and the claim.
     *
     * That yield is the whole substance of the race and is not a device to make the
     * test fail: in a real tab there is always real time between reading shared
     * storage and finishing the write — script evaluation, a React render, the
     * storage call itself. Two tabs are two event loops, so the other tab's read can
     * land inside that gap. A lock closes the gap by keeping the second document out
     * until the first has left; without one, both read the empty store and both mint.
     */
    function runnerFor(store: ReceiptStore, inner: ExclusiveSection): ExclusiveSection {
      const entry = documents.find((candidate) => candidate.store === store)!;
      return (name, body) =>
        inner(name, async () => {
          entry.snapshot = new Map(backing);
          await Promise.resolve();
          return body();
        });
    }

    return { document, runnerFor, backing };
  }

  it('mints twice when two documents claim without exclusion', async () => {
    // The negative control, and the justification for the mechanism. Both tabs read
    // before either writes, so both mint, and the two submissions carry different
    // identities — a server deduplicating perfectly still builds two workspaces,
    // because nobody told it the requests were one.
    const world = twoDocuments();
    const mint = minter();
    const storeA = world.document();
    const storeB = world.document();
    const noExclusion: ExclusiveSection = async (_name, body) => body();

    const [first, second] = await Promise.all([
      claimIdentityExclusive(
        storeA, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, world.runnerFor(storeA, noExclusion),
      ),
      claimIdentityExclusive(
        storeB, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, world.runnerFor(storeB, noExclusion),
      ),
    ]);

    expect(keyOf(first)).not.toBe(keyOf(second));
    // Recorded rather than implied: without a real cross-document lock the client
    // cannot close this, and `exclusiveSectionIsCrossTab` is how a caller finds out
    // whether it has one.
    expect(typeof exclusiveSectionIsCrossTab()).toBe('boolean');
  });

  it('mints once when the same two documents claim under a shared lock', async () => {
    // Identical to the previous test except for the section runner. The second
    // document enters after the first has written, refreshes its view, finds the
    // record and resumes — one identity, one workspace. That the ONLY difference is
    // the exclusion is what makes this pair evidence rather than assertion.
    const world = twoDocuments();
    const mint = minter();
    const storeA = world.document();
    const storeB = world.document();
    const shared = serializing();

    const [first, second] = await Promise.all([
      claimIdentityExclusive(
        storeA, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, world.runnerFor(storeA, shared),
      ),
      claimIdentityExclusive(
        storeB, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, world.runnerFor(storeB, shared),
      ),
    ]);

    expect(keyOf(first)).toBe('key-1');
    expect(keyOf(second)).toBe('key-1');
    expect(world.backing.size).toBe(1);
  });

  it('holds the second caller out until the first has written, given a lock', async () => {
    // The same interleaving, with a real serializing section. The second body does
    // not start until the first has finished writing, so it reads the first's
    // record and resumes — one identity, one workspace.
    const store = memoryReceiptStore();
    const mint = minter();
    const section = serializing();
    const order: string[] = [];
    const observed: ExclusiveSection = (name, body) =>
      section(name, async () => {
        order.push('enter');
        const result = await body();
        order.push('exit');
        return result;
      });

    const [first, second] = await Promise.all([
      claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, observed),
      claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, observed),
    ]);

    // Never two enters in a row: that is what "exclusive" means, asserted rather
    // than inferred from the keys happening to match.
    expect(order).toEqual(['enter', 'exit', 'enter', 'exit']);
    expect(keyOf(second)).toBe(keyOf(first));
  });

  it('serializes a claim that arrives while another is suspended', async () => {
    // Guards against a runner that is exclusive only for synchronous bodies. The
    // first claim is made to span a turn of the event loop; the second must still
    // wait, or every real async store (IndexedDB, a native bridge) reopens the race.
    const store = memoryReceiptStore();
    const mint = minter();
    const section = serializing();
    const slow: ExclusiveSection = (name, body) =>
      section(name, async () => {
        await Promise.resolve();
        return body();
      });

    const [first, second] = await Promise.all([
      claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, slow),
      claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, slow),
    ]);

    expect(keyOf(first)).toBe('key-1');
    expect(keyOf(second)).toBe('key-1');
  });

  it('still reports a conflict for a genuinely different payload', async () => {
    // Exclusion must not paper over the case it is not for: a changed payload
    // under a live identity is a different intent and stays a conflict, because
    // only the user can say whether the earlier request should be replaced.
    const store = memoryReceiptStore();
    const mint = minter();
    const section = serializing();

    await claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, section);
    const changed = await claimIdentityExclusive(
      store,
      SCOPE,
      INTENT,
      { ...PAYLOAD, name: 'different' },
      mint,
      NOW_ISO,
      section,
    );

    expect(changed.kind).toBe('conflict');
  });

  it('keeps organizations on separate locks so one does not block the other', async () => {
    // Two organizations claiming at once are two operations. Sharing a lock name
    // across them would serialize unrelated tenants and, worse, invite a reader to
    // conclude the records are shared.
    const store = memoryReceiptStore();
    const mint = minter();
    const seen: string[] = [];
    const section: ExclusiveSection = async (name, body) => {
      seen.push(name);
      return body();
    };

    await claimIdentityExclusive(store, SCOPE, INTENT, PAYLOAD, mint, NOW_ISO, section);
    await claimIdentityExclusive(store, OTHER_ORG, INTENT, PAYLOAD, mint, NOW_ISO, section);

    expect(new Set(seen).size).toBe(2);
    expect(seen[0]).toContain('org-1');
    expect(seen[1]).toContain('org-2');
  });
});


it('allows draft edits but preserves an identity once approval has been requested', async () => {
  const store = memoryReceiptStore();
  const section = async <T,>(_name: string, body: () => T | Promise<T>) => await body();
  const first = await claimPreviewIdentity(store, SCOPE, INTENT, { name: 'first' }, () => 'request-1', 'now', section);
  expect(first.kind).toBe('new');
  const edited = await claimPreviewIdentity(store, SCOPE, INTENT, { name: 'edited' }, () => 'request-2', 'now', section);
  expect(edited.kind).toBe('new');
  await markSubmissionStage(store, SCOPE, INTENT, 'request-2', 'approval', 'approval-2', section);
  const conflicting = await claimPreviewIdentity(store, SCOPE, INTENT, { name: 'third' }, () => 'request-3', 'now', section);
  expect(conflicting.kind).toBe('conflict');
  expect(readReceipt(store, SCOPE, INTENT)).toMatchObject({ idempotencyKey: 'request-2', approvalId: 'approval-2' });
});
