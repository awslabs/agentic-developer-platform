/**
 * The worker-side abort executor, end to end through the real store — Issue #3963 (S4).
 *
 * `control-abort-sentinel.test.ts` covers the sentinel document's shape. This file
 * covers the step before it: what `applyControlCommand` does when an authorized
 * abort is handed to it, driven through a real `ControlStateStore` rather than a
 * stub, because three of the four claims below are about *ordering* between the
 * store and the executor and a stub cannot have an order.
 *
 * The organising claim is the inverse of the sentinel suite's. There, the channel
 * may lose an abort but may never invent one. Here, the run must stop
 * unconditionally — cancellation is not contingent on anything — while the
 * *report* that it was deliberately stopped is contingent on evidence. Those two
 * must be able to disagree, and the journal has to say which happened.
 *
 * What this file deliberately does not do is verify a signature. Nothing in this
 * image can sign an envelope, so a test that minted one would be testing a
 * capability the pod does not have. The signature checks live in
 * `tests/test_abort_authorization.py` (worker, Python) and
 * `tests/agentauth/test_abort_receipt.py` (gateway), both against per-run keys.
 */
import { readFileSync } from 'fs';
import { join } from 'path';

import { applyControlCommand } from './control-command-apply';
import { ControlStateStore } from './control-state';
import { IMPLEMENTED_CONTROL_VERBS } from './control-runtime';
import type { ControlAction } from './control-state';

const COMMAND = 'cmd-abort-1';

/** The three journal fields the executor reads while a command is still delivered. */
const ENVELOPE = 'adpe1.eyJhY3Rpb24iOiJhYm9ydCJ9.ZW52ZWxvcGUtc2lnbmF0dXJl';
const SIGNED_BODY = Buffer.from(
  JSON.stringify({ command_id: COMMAND, reason: 'wrong issue' }),
  'utf8',
).toString('base64');
const RECEIPT = 'adpe1.eyJhY3Rpb24iOiJhYm9ydF9hY2NlcHRlZCJ9.cmVjZWlwdC1zaWduYXR1cmU';

/**
 * A store with abort supported and a revalidator that returns a receipt.
 *
 * `supportedActions` is the REAL implementation set, not a hand-written
 * `new Set(['abort'])`. That matters: if #3963's policy work were reverted and
 * `abort` left `IMPLEMENTED_CONTROL_VERBS`, a literal here would keep this suite
 * green while the live listener answered 501 to every abort. Reading the shipped
 * constant makes the suite fail in that case, which is the point.
 */
const storeWithAbort = (options: { receipt?: string | null } = {}) =>
  new ControlStateStore({
    generation: 4,
    supportedActions: IMPLEMENTED_CONTROL_VERBS,
    revalidate: async () => ({
      allowed: true,
      abortReceipt: options.receipt === undefined ? RECEIPT : options.receipt,
    }),
  });

/** Adapter double recording the single stop, with no SDK behind it. */
const adapterDouble = () => {
  const calls: string[] = [];
  return {
    calls,
    requestPause: async () => ({ state: 'paused' }) as never,
    resumeFromPause: async () => {},
    cancel: (reason: string) => {
      calls.push(`cancel:${reason}`);
    },
  };
};

/**
 * Put an authorized abort into the store the way the listener does.
 *
 * Goes through `submit` + `deliverAuthorized` rather than reaching into the
 * journal, so the executor reads the same `delivered`-gated fields it reads in
 * production. `deliverAuthorized` is also the only path that stores the receipt,
 * which is exactly the coupling under test in the last describe block.
 */
const deliverAbort = async (store: ControlStateStore): Promise<boolean> => {
  const outcome = store.submit('abort' as ControlAction, COMMAND, 'fingerprint-1', {
    envelope: ENVELOPE,
    action: 'abort',
    command_id: COMMAND,
    body_base64: SIGNED_BODY,
  });
  expect(outcome.kind).toBe('accepted');
  return store.deliverAuthorized(COMMAND, () => {});
};

describe('the run stops whether or not the abort can be reported', () => {
  it('cancels the attempt and settles applied when the record lands', async () => {
    const store = storeWithAbort();
    const adapter = adapterDouble();
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter,
      store,
      recordAbort: () => true,
    });

    expect(adapter.calls).toEqual(['cancel:run aborted by operator']);
    expect(store.lookup(COMMAND).status).toBe('applied');
  });

  it('still cancels when the record does NOT land, and says so', async () => {
    // The disagreement this suite exists for. Cancellation is unconditional, so a
    // failed record must not become a failed stop — an operator who asked for a
    // stop and got "we could not write a file, so we kept going" would be the
    // worst of the four outcomes.
    const store = storeWithAbort();
    const adapter = adapterDouble();
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter,
      store,
      recordAbort: () => false,
    });

    expect(adapter.calls).toEqual(['cancel:run aborted by operator']);
    // `unknown`, not `applied`: the run stopped but will be finalized by exit
    // code, so claiming the outcome was recorded would be a lie the operator
    // reads back from the journal.
    expect(store.lookup(COMMAND).status).toBe('unknown');
    expect(store.lookup(COMMAND).reason).toContain('could not be recorded');
  });

  it('writes the record BEFORE cancelling, not after', async () => {
    // Order is the requirement, not an implementation detail. `cancel()` starts
    // teardown; a record written afterwards may never get the chance to run, and
    // an abort whose record never landed finalizes as a crash. Asserted by
    // interleaving into one list, because two separate spies cannot express
    // "which came first".
    const order: string[] = [];
    const store = storeWithAbort();
    const adapter = {
      requestPause: async () => ({ state: 'paused' }) as never,
      resumeFromPause: async () => {},
      cancel: () => order.push('cancel'),
    };
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter,
      store,
      recordAbort: () => {
        order.push('record');
        return true;
      },
    });

    expect(order).toEqual(['record', 'cancel']);
  });

  it('shows abort_requested to an operator watching, before the process dies', async () => {
    const store = storeWithAbort();
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: () => true,
    });

    expect(store.snapshot().state).toBe('abort_requested');
  });
});

describe('what the executor hands the recorder', () => {
  it('passes the gateway receipt, the envelope and the signed bytes', async () => {
    const seen: Array<Record<string, unknown>> = [];
    const store = storeWithAbort();
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: (input) => {
        seen.push({ ...input });
        return true;
      },
    });

    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({
      commandId: COMMAND,
      envelope: ENVELOPE,
      signedBodyBase64: SIGNED_BODY,
      abortReceipt: RECEIPT,
    });
  });

  it('never forwards an unsigned reason, even when one is supplied', async () => {
    // The substitution this omission closes: the finalizer prints the operator's
    // words, and it must read them out of the bytes the gateway signed. A second
    // unsigned copy travelling beside the signature is how fabricated text came to
    // be attributed to a human under an otherwise-valid envelope. So `reason` must
    // be absent from the recorder's input — not merely equal to the signed text.
    const seen: Array<Record<string, unknown>> = [];
    const store = storeWithAbort();
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: (input) => {
        seen.push({ ...input });
        return true;
      },
      reason: 'a reason nobody signed',
    });

    expect(seen[0]).not.toHaveProperty('reason');
    expect(JSON.stringify(seen[0])).not.toContain('a reason nobody signed');
  });

  it('records no receipt when revalidation returned none', async () => {
    // Fails closed on absence rather than substituting a placeholder. The Python
    // reader refuses a sentinel with no receipt, so this run stops and is
    // finalized by exit code — losing the abort report, which is the safe
    // direction. Inventing a receipt field would be the unsafe one.
    const seen: Array<Record<string, unknown>> = [];
    const store = storeWithAbort({ receipt: null });
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: (input) => {
        seen.push({ ...input });
        return true;
      },
    });

    expect(seen[0].abortReceipt).toBeNull();
    // The envelope still travels: it proves an abort of this run was authorized,
    // which is a different claim from "this run accepted it" and is still true.
    expect(seen[0].envelope).toBe(ENVELOPE);
  });
});

describe('other queued commands do not outlive the abort', () => {
  it('cancels a pending pause rather than applying it to a stopping run', async () => {
    const store = storeWithAbort();
    store.submit('pause' as ControlAction, 'cmd-pause-1', 'fingerprint-p');
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: () => true,
    });

    const pause = store.lookup('cmd-pause-1');
    // `cancelled`, not `rejected`: the run did not refuse the pause, it stopped
    // before reaching it. An operator reading `rejected` would look for a
    // permission problem that never existed.
    expect(pause.status).toBe('cancelled');
    expect(pause.reason).toContain('aborted');
  });

  it('leaves the abort command itself applied, not cancelled by its own sweep', async () => {
    // The sweep skips `commandId`. Without that skip the abort would cancel
    // itself, and the journal would show the stop as never having been applied.
    const store = storeWithAbort();
    await deliverAbort(store);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: () => true,
    });

    expect(store.lookup(COMMAND).status).toBe('applied');
  });
});

describe('an abort cannot be delivered without live authorization', () => {
  it('is refused, unrecorded and unstopped when revalidation says no', async () => {
    // The listener routes proof-bearing verbs through `deliverAuthorized` and
    // never calls the executor if it returns false. This asserts the whole chain:
    // a refused re-check settles the command `rejected`, so the executor is never
    // reached, so nothing cancels and nothing is recorded.
    const store = new ControlStateStore({
      generation: 4,
      supportedActions: IMPLEMENTED_CONTROL_VERBS,
      revalidate: async () => false,
    });
    let handedOff = false;
    const outcome = store.submit('abort' as ControlAction, COMMAND, 'fingerprint-1', {
      envelope: ENVELOPE,
      action: 'abort',
      command_id: COMMAND,
      body_base64: SIGNED_BODY,
    });
    expect(outcome.kind).toBe('accepted');

    const delivered = await store.deliverAuthorized(COMMAND, () => {
      handedOff = true;
    });

    expect(delivered).toBe(false);
    expect(handedOff).toBe(false);
    expect(store.lookup(COMMAND).status).toBe('rejected');
  });

  it('exposes no receipt for a command that was never delivered', async () => {
    // A refused abort must hand the sentinel writer nothing. Note this passes even
    // without the `delivered` gate, because a refused re-check never stores a
    // receipt in the first place — the gate's real job is the test below.
    const store = new ControlStateStore({
      generation: 4,
      supportedActions: IMPLEMENTED_CONTROL_VERBS,
      revalidate: async () => false,
    });
    store.submit('abort' as ControlAction, COMMAND, 'fingerprint-1', {
      envelope: ENVELOPE,
      action: 'abort',
      command_id: COMMAND,
      body_base64: SIGNED_BODY,
    });
    await store.deliverAuthorized(COMMAND, () => {});

    expect(store.abortAcceptanceReceipt(COMMAND)).toBeNull();
    expect(store.authorizationProof(COMMAND)).toBeNull();
  });

  it('stops exposing the proof once the command has settled', async () => {
    // What the `delivered`-only restriction actually buys, and the case the test
    // above does NOT cover: all three reads are half of a bearer proof, and
    // returning them after settlement would let a later caller reconstruct an
    // authorization that has already been used. The window is exactly the handoff.
    //
    // Written as a separate test after a mutation run: deleting the `delivered`
    // check from `abortAcceptanceReceipt` left the never-delivered test above green,
    // because a refused command has no receipt to leak. Only a command that
    // genuinely held one and then settled can detect it.
    const store = storeWithAbort();
    await deliverAbort(store);

    // Available during the handoff window — this is the state the executor reads.
    expect(store.abortAcceptanceReceipt(COMMAND)).toBe(RECEIPT);
    expect(store.authorizationProof(COMMAND)).toBe(ENVELOPE);
    expect(store.signedRequestBody(COMMAND)).toBe(SIGNED_BODY);

    await applyControlCommand({
      action: 'abort' as ControlAction,
      commandId: COMMAND,
      adapter: adapterDouble(),
      store,
      recordAbort: () => true,
    });

    expect(store.lookup(COMMAND).status).toBe('applied');
    expect(store.abortAcceptanceReceipt(COMMAND)).toBeNull();
    expect(store.authorizationProof(COMMAND)).toBeNull();
    expect(store.signedRequestBody(COMMAND)).toBeNull();
  });
});

/**
 * Root's legacy-path question, answered as executable control flow rather than prose.
 *
 * The review asked whether a run on the legacy (non-authority) path can accept an
 * abort, and required that if it cannot, the refusal be proven through the actual
 * entrypoint/control flow rather than asserted. It cannot, and the refusal is
 * structural at three independent points. The first two are checked here; the
 * third is an environment gate in `control-revalidation.ts` and is checked by the
 * assertion on its source, because exercising it would require a live endpoint.
 */
describe('a legacy run cannot accept an abort', () => {
  it('requires an envelope for abort BECAUSE abort is implemented', () => {
    // `ControlListener.requiresEnvelope` returns `store.isSupported(action)`. That
    // derivation is the property: enabling a verb and demanding proof for it are
    // one decision, so there is no configuration in which abort is performable and
    // unauthenticated. A separate opt-in list is exactly what this avoids.
    const store = storeWithAbort();

    expect(IMPLEMENTED_CONTROL_VERBS.has('abort' as ControlAction)).toBe(true);
    expect(store.isSupported('abort' as ControlAction)).toBe(true);

    // This test used to name `steer` as its control — an implemented verb paired
    // with an unimplemented one, showing the two answers tracking each other.
    // Issue #3965 implements `steer`, so that control is gone, and the honest
    // repair is to assert the derivation over the whole verb set rather than to
    // find another verb to stand in. This is strictly stronger than the original
    // pair: it holds no matter which verbs are implemented, so it cannot be
    // invalidated again the next time one is enabled.
    const everyVerb: ControlAction[] = ['pause', 'resume', 'steer', 'abort'];
    for (const verb of everyVerb) {
      expect(store.isSupported(verb)).toBe(IMPLEMENTED_CONTROL_VERBS.has(verb));
    }

    // And the other half of the derivation, which is what made the original
    // control safe: an unsupported verb is unauthenticated *and* undeliverable.
    // Constructed with an empty supported set rather than by naming a verb,
    // because there is no longer an unimplemented verb to name.
    const legacy = new ControlStateStore({ generation: 4, supportedActions: new Set<ControlAction>() });
    for (const verb of everyVerb) {
      expect(legacy.isSupported(verb)).toBe(false);
      expect(legacy.submit(verb, `${COMMAND}-${verb}`, 'fingerprint-x').kind).toBe('unsupported');
    }
  });

  it('cannot deliver an abort when no revalidator is configured', async () => {
    // A legacy run has no authority endpoint, so `revalidate` is effectively
    // absent. `deliverAuthorized` awaits it and treats `undefined` as not-allowed,
    // so the command settles `rejected` and the executor is never reached. This is
    // the second structural refusal, and it holds with no flag and no gateway.
    const store = new ControlStateStore({
      generation: 4,
      supportedActions: IMPLEMENTED_CONTROL_VERBS,
    });
    store.submit('abort' as ControlAction, COMMAND, 'fingerprint-1', {
      envelope: ENVELOPE,
      action: 'abort',
      command_id: COMMAND,
      body_base64: SIGNED_BODY,
    });

    const delivered = await store.deliverAuthorized(COMMAND, () => {});

    expect(delivered).toBe(false);
    expect(store.lookup(COMMAND).status).toBe('rejected');
  });

  it('gates the authority call on ADP_AGENT_AUTHORITY_ENABLED', () => {
    // The third refusal. `postRevalidation` throws unless the flag is exactly
    // 'true' AND the endpoint matches the API Gateway pattern, and
    // `revalidateQueuedCommand` catches that into `false`. Asserted on the source
    // because reaching it needs a live signed endpoint; what must not regress is
    // that the flag is compared, not merely read with a default.
    const source = readFileSync(join(__dirname, 'control-revalidation.ts'), 'utf8');

    expect(source).toContain("process.env.ADP_AGENT_AUTHORITY_ENABLED !== 'true'");
    // Fails closed into a refusal, never into an approval.
    expect(source).toContain('catch { return false; }');
  });
});
