/**
 * The worker half of the envelope contract (#5028 AC5).
 *
 * Most of this file is one parametrized test over
 * `src/__fixtures__/control-envelope-vectors.json` — the same bytes the gateway
 * suite verifies in `modules/gateway/tests/agentauth/test_envelope_vectors.py`.
 * That shared fixture is the only thing that stops the two verifiers from
 * drifting apart while both suites stay green, so vectors are read from it rather
 * than signed here.
 *
 * The hand-written tests below it cover what a shared fixture cannot: key
 * parsing from the pod's environment string, and the guarantee that this module
 * has no signing capability at all.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { generateKeyPairSync, sign as cryptoSign } from 'node:crypto';

import {
  ALLOWED_ALGORITHMS,
  ENVELOPE_AUDIENCE,
  ENVELOPE_ISSUER,
  ENVELOPE_VERSION,
  MAX_ENVELOPE_TTL_SECONDS,
  MODEL_POLICY_AUDIENCE,
  bodyDigest,
  parseVerificationKeys,
  verifyEnvelope,
  type EnvelopeFailure,
} from './control-envelope';

interface Vector {
  name: string;
  accept: boolean;
  reason?: EnvelopeFailure;
  token: string;
  note: string;
}

interface Fixture {
  version: string;
  now: string;
  public_keys: Record<string, string>;
  expected: {
    run_id: string;
    generation: number;
    action: string;
    command_id: string;
    body: string;
  };
  vectors: Vector[];
}

const FIXTURE_PATH = join(__dirname, '__fixtures__', 'control-envelope-vectors.json');
const fixture: Fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'));

const keys = parseVerificationKeys(
  Object.entries(fixture.public_keys)
    .map(([kid, value]) => `${kid}:${value}`)
    .join(','),
);
const nowMs = Date.parse(fixture.now);

function verify(token: string | undefined) {
  return verifyEnvelope(token, keys, {
    runId: fixture.expected.run_id,
    generation: fixture.expected.generation,
    action: fixture.expected.action,
    commandId: fixture.expected.command_id,
    body: Buffer.from(fixture.expected.body, 'utf8'),
    nowMs,
  });
}

describe('shared envelope vectors', () => {
  it('agrees with the gateway on the fixture version', () => {
    // A version bump on one side and not the other would make every vector
    // fail as "malformed", which looks like a broken verifier rather than a
    // stale fixture. Naming it here makes the real cause obvious.
    expect(fixture.version).toBe(ENVELOPE_VERSION);
  });

  it('loaded the verification keys the fixture ships', () => {
    // Guards against the whole suite passing for the wrong reason: with an empty
    // key map every vector is refused, including the one that must be accepted.
    expect(keys.size).toBe(Object.keys(fixture.public_keys).length);
  });

  it.each(fixture.vectors.map((v) => [v.name, v] as const))('%s', (_name, vector) => {
    const result = verify(vector.token);

    if (vector.accept) {
      expect(result.ok).toBe(true);
      if (!result.ok) return;
      expect(result.envelope.targetRunId).toBe(fixture.expected.run_id);
      expect(result.envelope.targetGeneration).toBe(fixture.expected.generation);
      expect(result.envelope.action).toBe(fixture.expected.action);
      expect(result.envelope.commandId).toBe(fixture.expected.command_id);
      expect(result.envelope.bodyDigest).toBe(bodyDigest(Buffer.from(fixture.expected.body, 'utf8')));
      return;
    }

    expect(result.ok).toBe(false);
    if (result.ok) return;
    // The reason is asserted, not just the refusal. A vector refused for the
    // wrong reason means the check it was written to exercise is being masked by
    // an earlier one and is no longer proven to work.
    expect(result.reason).toBe(vector.reason);
  });

  it('has a vector for every failure reason this module can return', () => {
    const covered = new Set(fixture.vectors.filter((v) => !v.accept).map((v) => v.reason));
    const all: EnvelopeFailure[] = [
      'malformed',
      'unsupported_algorithm',
      'untrusted_issuer',
      'audience_mismatch',
      'unknown_key',
      'bad_signature',
      'target_mismatch',
      'generation_mismatch',
      'action_mismatch',
      'command_mismatch',
      'body_mismatch',
      'expired',
      'not_yet_valid',
      'validity_too_long',
    ];
    expect(all.filter((reason) => !covered.has(reason))).toEqual([]);
  });
});

describe('binding against the listener own facts', () => {
  /**
   * The vectors vary the envelope while holding the listener's facts fixed.
   * These invert it: one genuinely valid envelope, and a listener whose own facts
   * differ. That is the real production shape — a replayed envelope arrives
   * unchanged at the wrong pod.
   */
  const valid = fixture.vectors.find((v) => v.accept)!.token;

  function verifyAgainst(overrides: Partial<Parameters<typeof verifyEnvelope>[2]>) {
    return verifyEnvelope(valid, keys, {
      runId: fixture.expected.run_id,
      generation: fixture.expected.generation,
      action: fixture.expected.action,
      commandId: fixture.expected.command_id,
      body: Buffer.from(fixture.expected.body, 'utf8'),
      nowMs,
      ...overrides,
    });
  }

  it('refuses a valid envelope arriving at a different run', () => {
    const result = verifyAgainst({ runId: 'run-a-different-pod' });
    expect(result).toEqual({ ok: false, reason: 'target_mismatch' });
  });

  it('refuses a valid envelope after the pod restarted into a new generation', () => {
    const result = verifyAgainst({ generation: fixture.expected.generation + 1 });
    expect(result).toEqual({ ok: false, reason: 'generation_mismatch' });
  });

  it('refuses an envelope presented at a different action path', () => {
    const result = verifyAgainst({ action: 'abort' });
    expect(result).toEqual({ ok: false, reason: 'action_mismatch' });
  });

  it('refuses an envelope replayed with a fresh command id', () => {
    const result = verifyAgainst({ commandId: 'cmd-0002' });
    expect(result).toEqual({ ok: false, reason: 'command_mismatch' });
  });

  it('refuses a body edited in transit after authorization', () => {
    const result = verifyAgainst({
      body: Buffer.from('{"command_id":"cmd-0001","reason":"budget review "}', 'utf8'),
    });
    expect(result).toEqual({ ok: false, reason: 'body_mismatch' });
  });

  it('digests raw bytes so a reserialized equivalent body is still refused', () => {
    // Key-order-different but semantically identical JSON. Accepting it would
    // mean the digest was over a parsed object, which is the loophole that lets
    // a proxy rewrite an authorized command.
    const result = verifyAgainst({
      body: Buffer.from('{"reason":"budget review","command_id":"cmd-0001"}', 'utf8'),
    });
    expect(result).toEqual({ ok: false, reason: 'body_mismatch' });
  });

  it('refuses once the short window has passed', () => {
    const result = verifyAgainst({ nowMs: nowMs + (MAX_ENVELOPE_TTL_SECONDS + 1) * 1000 });
    expect(result).toEqual({ ok: false, reason: 'expired' });
  });

  it('accepts inside the window', () => {
    expect(verifyAgainst({ nowMs: nowMs + 1000 }).ok).toBe(true);
  });

  it('refuses exactly at expiry rather than treating the boundary as valid', () => {
    const result = verifyAgainst({ nowMs: nowMs + MAX_ENVELOPE_TTL_SECONDS * 1000 });
    expect(result).toEqual({ ok: false, reason: 'expired' });
  });

  it('refuses a missing envelope header', () => {
    expect(verifyEnvelope(undefined, keys, {
      runId: fixture.expected.run_id,
      generation: fixture.expected.generation,
      action: fixture.expected.action,
      commandId: fixture.expected.command_id,
      body: Buffer.from(fixture.expected.body, 'utf8'),
      nowMs,
    })).toEqual({ ok: false, reason: 'malformed' });
  });

  it('refuses an empty envelope header', () => {
    expect(verify('')).toEqual({ ok: false, reason: 'malformed' });
  });

  it('refuses an oversized token before parsing it', () => {
    expect(verify(`${ENVELOPE_VERSION}.${'A'.repeat(9000)}.AA`)).toEqual({
      ok: false,
      reason: 'malformed',
    });
  });

  it('refuses a valid envelope when the listener holds no keys at all', () => {
    // The fail-closed default. A pod that never received a verification key must
    // refuse control commands, not accept them unverified.
    const result = verifyEnvelope(valid, new Map(), {
      runId: fixture.expected.run_id,
      generation: fixture.expected.generation,
      action: fixture.expected.action,
      commandId: fixture.expected.command_id,
      body: Buffer.from(fixture.expected.body, 'utf8'),
      nowMs,
    });
    expect(result).toEqual({ ok: false, reason: 'unknown_key' });
  });
});

describe('model-policy audience and chain binding', () => {
  const pair = generateKeyPairSync('ed25519');
  const policyKeys = parseVerificationKeys(JSON.stringify({
    policy: pair.publicKey.export({ format: 'pem', type: 'spki' }),
  }));
  const policyBody = Buffer.from('{"resolved_model_id":"global.anthropic.claude-sonnet-4-6"}', 'utf8');
  const payload = {
    v: ENVELOPE_VERSION,
    iss: ENVELOPE_ISSUER,
    aud: MODEL_POLICY_AUDIENCE,
    alg: 'ed25519',
    kid: 'policy',
    tenant_id: 'tenant-a',
    principal: 'run-a#1',
    target_run_id: 'run-a',
    target_generation: 1,
    action: 'resolve_model',
    command_id: 'snapshot-digest',
    body_digest: bodyDigest(policyBody),
    grant_id: 'grant-a',
    revocation_epoch: 1,
    chain_id: 'chain-a',
    iat: fixture.now,
    nbf: fixture.now,
    exp: new Date(nowMs + 30_000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
  };
  const raw = Buffer.from(JSON.stringify(payload, Object.keys(payload).sort()), 'utf8');
  const signature = cryptoSign(null, Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`), raw]), pair.privateKey);
  const token = `${ENVELOPE_VERSION}.${raw.toString('base64url')}.${signature.toString('base64url')}`;

  function verifyPolicy(chainId: string) {
    return verifyEnvelope(token, policyKeys, {
      runId: 'run-a',
      generation: 1,
      action: 'resolve_model',
      commandId: 'snapshot-digest',
      body: policyBody,
      audience: MODEL_POLICY_AUDIENCE,
      chainId,
      nowMs,
    });
  }

  it('accepts a decision only for its dedicated audience and chain', () => {
    expect(verifyPolicy('chain-a').ok).toBe(true);
  });

  it('refuses replay onto another chain', () => {
    expect(verifyPolicy('chain-b')).toEqual({ ok: false, reason: 'chain_mismatch' });
  });
});

describe('parseVerificationKeys', () => {
  const good = Object.entries(fixture.public_keys)[0];

  it('accepts the JSON/PEM map Terraform renders and excludes private or other algorithm keys', () => {
    const pair = generateKeyPairSync('ed25519');
    const other = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
    const parsed = parseVerificationKeys(JSON.stringify({
      current: pair.publicKey.export({ format: 'pem', type: 'spki' }),
      private: pair.privateKey.export({ format: 'pem', type: 'pkcs8' }),
      other: other.publicKey.export({ format: 'pem', type: 'spki' }),
    }));
    expect([...parsed.keys()]).toEqual(['current']);
  });

  it('parses the single-key form the pod normally receives', () => {
    const parsed = parseVerificationKeys(`${good[0]}:${good[1]}`);
    expect([...parsed.keys()]).toEqual([good[0]]);
  });

  it('parses two keys so a rotation needs no coordinated restart', () => {
    const other = generateKeyPairSync('ed25519').publicKey.export({ format: 'der', type: 'spki' });
    const otherRaw = Buffer.from(other.subarray(other.length - 32)).toString('base64');
    const parsed = parseVerificationKeys(`${good[0]}:${good[1]},next-key:${otherRaw}`);
    expect(parsed.size).toBe(2);
    expect(parsed.has('next-key')).toBe(true);
  });

  it('keeps the good key when a sibling entry is malformed', () => {
    // One bad entry must not take out a listener that also holds a usable key —
    // that would turn a config typo into a control outage.
    const parsed = parseVerificationKeys(`broken,${good[0]}:${good[1]},:novalue,alsobad:`);
    expect([...parsed.keys()]).toEqual([good[0]]);
  });

  it('rejects a key of the wrong length rather than constructing something unusable', () => {
    expect(parseVerificationKeys('short:AAAA').size).toBe(0);
  });

  it('rejects non-base64 garbage', () => {
    expect(parseVerificationKeys('bad:!!!!not base64!!!!').size).toBe(0);
  });

  it('returns an empty map for an unset variable', () => {
    expect(parseVerificationKeys(undefined).size).toBe(0);
    expect(parseVerificationKeys('').size).toBe(0);
  });

  it('trims whitespace around entries', () => {
    const parsed = parseVerificationKeys(` ${good[0]} : ${good[1]} `);
    expect(parsed.has(good[0])).toBe(true);
  });

  it('lets a later duplicate kid win rather than silently keeping both', () => {
    const parsed = parseVerificationKeys(`${good[0]}:${good[1]},${good[0]}:${good[1]}`);
    expect(parsed.size).toBe(1);
  });
});

describe('the worker cannot mint what it verifies', () => {
  it('exports exactly three functions, none of which signs', () => {
    // The security property in one assertion: this module is verification-only.
    // Pinned as an exact list rather than a name pattern, because a pattern both
    // misfires on constants like ENVELOPE_ISSUER and misses a signer named
    // something innocuous. If a signing helper is ever added here, the worker
    // becomes able to authorize its own commands and the envelope stops meaning
    // anything — so a new export has to be justified by editing this line.
    const module = require('./control-envelope') as Record<string, unknown>;
    const functions = Object.keys(module)
      .filter((name) => typeof module[name] === 'function')
      .sort();
    expect(functions).toEqual(['bodyDigest', 'parseVerificationKeys', 'verifyEnvelope']);
  });

  it('refuses an envelope a worker signed with its own freshly generated key', () => {
    // The concrete version of the above: a compromised worker generates a key
    // pair, signs a perfectly-shaped envelope, and presents it. It fails because
    // its kid is not one the listener trusts.
    const { privateKey } = generateKeyPairSync('ed25519');
    const payload = {
      v: ENVELOPE_VERSION,
      iss: ENVELOPE_ISSUER,
      aud: ENVELOPE_AUDIENCE,
      alg: 'ed25519',
      kid: 'attacker-key',
      tenant_id: 'org-tenant-001',
      principal: 'inv-attacker#1',
      target_run_id: fixture.expected.run_id,
      target_generation: fixture.expected.generation,
      action: fixture.expected.action,
      command_id: fixture.expected.command_id,
      body_digest: bodyDigest(Buffer.from(fixture.expected.body, 'utf8')),
      grant_id: 'grant-invented',
      revocation_epoch: 1,
      iat: fixture.now,
      nbf: fixture.now,
      exp: new Date(nowMs + 30_000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
    };
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const signature = cryptoSign(null, Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`), body]), privateKey);
    const forged = `${ENVELOPE_VERSION}.${body.toString('base64url')}.${signature.toString('base64url')}`;

    expect(verify(forged)).toEqual({ ok: false, reason: 'unknown_key' });
  });

  it('permits exactly one algorithm', () => {
    expect([...ALLOWED_ALGORITHMS]).toEqual(['ed25519']);
  });
});
