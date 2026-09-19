/**
 * Worker-side verification of the gateway's control authorization envelope (#5028).
 *
 * ## What this adds to the checks already here
 *
 * `control-listener.ts` already checks a bearer token, its expiry (#5024) and the
 * run generation. Those prove the caller knows a secret **this worker minted and
 * wrote to its own DynamoDB row**. They do not prove the gateway authorized this
 * particular command, and the shared worker IAM role can read and write those
 * rows — so a worker that reads another run's row learns that run's token and can
 * then satisfy every check in `authenticate()`.
 *
 * The envelope closes that. It is signed by the trusted control service with a
 * key no worker holds, and it is bound to this run, this generation, this action,
 * this command ID and these exact body bytes.
 *
 * ## Verification keys only
 *
 * Ed25519. The worker receives public keys through `ADP_CONTROL_ENVELOPE_KEYS`
 * and there is deliberately no code path here that signs anything. Node's
 * `crypto.verify(null, ...)` does Ed25519 natively, so this adds no dependency to
 * the worker image.
 *
 * The existing lineage-marker HMAC key is unsuitable for this job: workers hold
 * it in order to sign their own markers, and an authority whose key the attacker
 * holds is not an authority.
 *
 * ## `alg` is checked, never dispatched on
 *
 * The payload carries `alg`, and it is compared against a one-element allowlist
 * before any signature work happens. It exists so a future second algorithm is an
 * explicit change here, not so a caller can pick one. `alg: "none"` is simply not
 * in the list.
 *
 * ## What this cannot do
 *
 * A valid signature does not prove the gateway's decision was correct, and it
 * cannot protect this worker from its own compromised process. It also cannot
 * express revocation: an envelope stays valid for its short window regardless of
 * what happens to the grant behind it, which is why the window is 30 seconds and
 * why a queued action must be revalidated at the gateway before it takes effect.
 */

import { createHash, createPublicKey, verify as cryptoVerify, type KeyObject } from 'node:crypto';

/** Envelope version. Part of the signed input, checked before parsing. */
export const ENVELOPE_VERSION = 'adpe1';

/** The one permitted algorithm. An allowlist, not a dispatch table. */
export const ALLOWED_ALGORITHMS = new Set(['ed25519']);

/** Must match `src/agentauth/envelope.py` on the gateway side. */
export const ENVELOPE_ISSUER = 'adp-gateway-control';
export const ENVELOPE_AUDIENCE = 'adp-agent-control-listener';
export const MODEL_POLICY_AUDIENCE = 'adp-agent-model-policy';

/**
 * Maximum validity this worker will accept, in seconds. An envelope claiming
 * longer is rejected rather than truncated: accepting it would let the signer
 * overrule the revocation-delay bound the platform documents.
 */
export const MAX_ENVELOPE_TTL_SECONDS = 30;

/** Bound before any parsing. A real envelope is well under this. */
const MAX_ENVELOPE_BYTES = 8192;

const REQUIRED_CLAIMS = [
  'iss',
  'aud',
  'alg',
  'kid',
  'tenant_id',
  'principal',
  'target_run_id',
  'target_generation',
  'action',
  'command_id',
  'body_digest',
  'iat',
  'nbf',
  'exp',
] as const;

/**
 * Claims whose JSON type is part of the contract, checked as a group before any
 * claim is used in a Set lookup, a Map lookup or a `String(...)` coercion.
 *
 * This mirrors `_STRING_CLAIMS` / `_INT_CLAIMS` in `src/agentauth/envelope.py`
 * and fixes a real defect on this side too: `String(payload.kid)` on a payload
 * carrying `"kid": {"toString": 0}` throws `TypeError: Cannot convert object to
 * primitive value`, because the object shadows `toString` with a non-callable.
 * The listener's outer catch keeps the process alive but answers with an
 * internal error instead of the opaque authorization refusal this contract
 * promises — so the input is distinguishable from a merely-unauthorized one, and
 * the local reason log is lost.
 *
 * Neither language's defect admitted anything: nothing verified that should have
 * been refused. Both were refusal paths that could throw.
 *
 * Types are required rather than coerced. `String(value)` on hostile JSON is a
 * silent accept — `{"kid": ["k1"]}` becomes the string `"k1"` in JS (Array
 * `toString` joins), which is a *different* value than Python's `"['k1']"`. Two
 * verifiers that coerce therefore disagree about what a malformed envelope even
 * says, which is precisely what the shared vectors exist to prevent.
 */
const STRING_CLAIMS = [
  'iss',
  'aud',
  'alg',
  'kid',
  'tenant_id',
  'principal',
  'target_run_id',
  'action',
  'command_id',
  'body_digest',
  'iat',
  'nbf',
  'exp',
] as const;

const INT_CLAIMS = ['target_generation'] as const;

/** ISO-8601 UTC seconds, the one format both sides emit. */
const TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/** A verified envelope. */
export interface ControlEnvelope {
  tenantId: string;
  principal: string;
  targetRunId: string;
  targetGeneration: number;
  action: string;
  commandId: string;
  bodyDigest: string;
  grantId?: string;
  revocationEpoch?: number;
  authorityKind?: 'delegated_grant' | 'human_session';
  keyId: string;
  issuedAt: number;
  notBefore: number;
  expiresAt: number;
  flowId?: string;
  authorityReferenceId?: string;
  chainId?: string;
}

/**
 * Why an envelope was refused.
 *
 * A discriminated result rather than a thrown error, because the listener has to
 * respond with one opaque status regardless of reason — and having the reason as
 * a value makes it loggable locally without becoming part of the HTTP response.
 */
export type EnvelopeFailure =
  | 'malformed'
  | 'unsupported_algorithm'
  | 'untrusted_issuer'
  | 'audience_mismatch'
  | 'unknown_key'
  | 'bad_signature'
  | 'target_mismatch'
  | 'generation_mismatch'
  | 'action_mismatch'
  | 'command_mismatch'
  | 'body_mismatch'
  | 'chain_mismatch'
  | 'expired'
  | 'not_yet_valid'
  | 'validity_too_long';

export type EnvelopeResult =
  | { ok: true; envelope: ControlEnvelope }
  | { ok: false; reason: EnvelopeFailure };

/** What the verifier already knows independently of the envelope. */
export interface ExpectedBinding {
  runId: string;
  generation: number;
  action: string;
  commandId: string;
  /** The raw body bytes as received off the socket — not a re-serialized object. */
  body: Buffer;
  /** Defaults to the control-listener audience for backward compatibility. */
  audience?: string;
  /** Required for a chain-bound model decision; absent for control commands. */
  chainId?: string;
  /** Injectable for tests; milliseconds since epoch. */
  nowMs?: number;
}

/**
 * Parse `ADP_CONTROL_ENVELOPE_KEYS` into a keyId -> public key map.
 *
 * Format: `kid1:<base64 raw 32-byte key>,kid2:<...>`. Multiple keys are the
 * rotation story — a listener holds the outgoing and incoming key at once and
 * selects by `kid` rather than trying each in turn, so a rotation needs no
 * coordinated restart.
 *
 * Malformed entries are skipped rather than failing the whole map: one bad entry
 * must not take out a listener that also holds a good key. A map that ends up
 * empty means no envelope can verify, which is the correct fail-closed outcome.
 */
export function parseVerificationKeys(raw: string | undefined): Map<string, KeyObject> {
  const keys = new Map<string, KeyObject>();
  if (!raw) return keys;

  // Terraform supplies a JSON map of PEM public keys. Keep the compact raw-key
  // form for existing deployments and fixtures, but accept the actual manifest.
  if (raw.trimStart().startsWith('{')) {
    try {
      const entries = JSON.parse(raw);
      if (!entries || Array.isArray(entries)) return keys;
      for (const [kid, pem] of Object.entries(entries)) {
        if (!kid || typeof pem !== 'string' || !pem.startsWith('-----BEGIN PUBLIC KEY-----')) continue;
        try {
          const key = createPublicKey(pem);
          if (key.asymmetricKeyType === 'ed25519') keys.set(kid, key);
        } catch { continue; }
      }
    } catch { return keys; }
    return keys;
  }

  for (const entry of raw.split(',')) {
    const separator = entry.indexOf(':');
    if (separator <= 0) continue;
    const kid = entry.slice(0, separator).trim();
    const encoded = entry.slice(separator + 1).trim();
    if (!kid || !encoded) continue;

    try {
      const rawKey = Buffer.from(encoded, 'base64');
      if (rawKey.length !== 32) continue;
      // Wrap the raw 32 bytes in the DER prefix for an Ed25519 SPKI so
      // createPublicKey accepts it. Shipping raw bytes in the env var keeps the
      // config readable; the prefix is a constant, not a parsed value.
      const der = Buffer.concat([
        Buffer.from('302a300506032b6570032100', 'hex'),
        rawKey,
      ]);
      keys.set(kid, createPublicKey({ key: der, format: 'der', type: 'spki' }));
    } catch {
      continue;
    }
  }
  return keys;
}

/** SHA-256 hex of the exact bytes received. */
export function bodyDigest(raw: Buffer): string {
  return createHash('sha256').update(raw).digest('hex');
}

function parseTimestamp(value: unknown): number | null {
  if (typeof value !== 'string' || !TIMESTAMP_RE.test(value)) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function isPlainInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

/**
 * Verify an envelope against locally-known facts.
 *
 * Order is deliberate and mirrors the Python implementation: size, version,
 * structure, required claims, algorithm/issuer/audience, key selection,
 * signature, then the binding checks, then validity. Nothing from the payload
 * influences anything beyond its own rejection until the signature has verified.
 */
export function verifyEnvelope(
  token: string | undefined,
  keys: Map<string, KeyObject>,
  expected: ExpectedBinding,
): EnvelopeResult {
  if (!token || token.length > MAX_ENVELOPE_BYTES) {
    return { ok: false, reason: 'malformed' };
  }

  const parts = token.split('.');
  if (parts.length !== 3 || parts[0] !== ENVELOPE_VERSION) {
    return { ok: false, reason: 'malformed' };
  }

  let body: Buffer;
  let signature: Buffer;
  let payload: Record<string, unknown>;
  try {
    body = Buffer.from(parts[1], 'base64url');
    signature = Buffer.from(parts[2], 'base64url');
    const decoded: unknown = JSON.parse(body.toString('utf8'));
    if (typeof decoded !== 'object' || decoded === null || Array.isArray(decoded)) {
      return { ok: false, reason: 'malformed' };
    }
    payload = decoded as Record<string, unknown>;
  } catch {
    return { ok: false, reason: 'malformed' };
  }

  if (payload.v !== ENVELOPE_VERSION) return { ok: false, reason: 'malformed' };
  for (const claim of REQUIRED_CLAIMS) {
    const value = payload[claim];
    if (value === undefined || value === null || value === '') {
      return { ok: false, reason: 'malformed' };
    }
  }

  // Claim types, before any claim is hashed, looked up or coerced. See
  // STRING_CLAIMS for why this precedes the checks below.
  for (const claim of STRING_CLAIMS) {
    if (typeof payload[claim] !== 'string') return { ok: false, reason: 'malformed' };
  }
  for (const claim of INT_CLAIMS) {
    if (!isPlainInteger(payload[claim])) return { ok: false, reason: 'malformed' };
  }

  // Legacy envelopes without a kind are delegated, and still require an epoch.
  const authorityKind = payload.authority_kind === undefined ? 'delegated_grant' : payload.authority_kind;
  if (authorityKind === 'human_session') {
    if (['grant_id', 'revocation_epoch', 'authority_reference_id'].some((claim) => claim in payload)) {
      return { ok: false, reason: 'malformed' };
    }
  } else if (authorityKind === 'delegated_grant') {
    if (typeof payload.grant_id !== 'string' || !payload.grant_id ||
        !isPlainInteger(payload.revocation_epoch) || payload.revocation_epoch < 1) {
      return { ok: false, reason: 'malformed' };
    }
  } else {
    return { ok: false, reason: 'malformed' };
  }

  if (!ALLOWED_ALGORITHMS.has(payload.alg as string)) {
    return { ok: false, reason: 'unsupported_algorithm' };
  }
  if (payload.iss !== ENVELOPE_ISSUER) return { ok: false, reason: 'untrusted_issuer' };
  if (payload.aud !== (expected.audience ?? ENVELOPE_AUDIENCE)) {
    return { ok: false, reason: 'audience_mismatch' };
  }

  const keyId = payload.kid as string;
  const key = keys.get(keyId);
  if (!key) return { ok: false, reason: 'unknown_key' };

  const signed = Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`, 'utf8'), body]);
  let signatureValid = false;
  try {
    signatureValid = cryptoVerify(null, signed, key, signature);
  } catch {
    signatureValid = false;
  }
  if (!signatureValid) return { ok: false, reason: 'bad_signature' };

  // Binding checks. Each compares the envelope's claim against a value this
  // process knows on its own — its run ID, its generation, the action from the
  // request path, the command ID from the parsed body, the raw body bytes.
  if (payload.target_run_id !== expected.runId) {
    return { ok: false, reason: 'target_mismatch' };
  }
  if (payload.target_generation !== expected.generation) {
    return { ok: false, reason: 'generation_mismatch' };
  }
  if (payload.action !== expected.action) return { ok: false, reason: 'action_mismatch' };
  if (payload.command_id !== expected.commandId) {
    return { ok: false, reason: 'command_mismatch' };
  }
  if (payload.body_digest !== bodyDigest(expected.body)) {
    return { ok: false, reason: 'body_mismatch' };
  }
  if (expected.chainId !== undefined && payload.chain_id !== expected.chainId) {
    return { ok: false, reason: 'chain_mismatch' };
  }

  const issuedAt = parseTimestamp(payload.iat);
  const notBefore = parseTimestamp(payload.nbf);
  const expiresAt = parseTimestamp(payload.exp);
  if (issuedAt === null || notBefore === null || expiresAt === null) {
    return { ok: false, reason: 'malformed' };
  }

  const now = expected.nowMs ?? Date.now();
  if (now >= expiresAt) return { ok: false, reason: 'expired' };
  if (now < notBefore) return { ok: false, reason: 'not_yet_valid' };
  if (expiresAt - notBefore > MAX_ENVELOPE_TTL_SECONDS * 1000) {
    return { ok: false, reason: 'validity_too_long' };
  }

  return {
    ok: true,
    envelope: {
      tenantId: payload.tenant_id as string,
      principal: payload.principal as string,
      targetRunId: payload.target_run_id as string,
      targetGeneration: payload.target_generation as number,
      action: payload.action as string,
      commandId: payload.command_id as string,
      bodyDigest: payload.body_digest as string,
      grantId: payload.grant_id as string | undefined,
      revocationEpoch: payload.revocation_epoch as number | undefined,
      authorityKind,
      keyId,
      issuedAt,
      notBefore,
      expiresAt,
      flowId: typeof payload.flow_id === 'string' ? payload.flow_id : undefined,
      authorityReferenceId:
        typeof payload.authority_reference_id === 'string'
          ? payload.authority_reference_id
          : undefined,
      chainId: typeof payload.chain_id === 'string' ? payload.chain_id : undefined,
    },
  };
}
