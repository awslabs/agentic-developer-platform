/**
 * The abort sentinel: how an aborting run tells the finalizing half what happened
 * — Issue #3963 (S4).
 *
 * A hosted run is two cooperating processes. This one (Node) holds the model
 * conversation, the control listener and the cancellation signal. The other
 * (`entrypoint.py`) owns the closing comment, the invocation status, the check-run
 * conclusion and the queue acknowledgement. Only the Python half can write the
 * invocation row, because only it holds both halves of that row's key
 * (`event_id` AND `arrived_at`); Node is given `ADP_MESSAGE_ID` alone. So an
 * abort decided here has to *travel* to there, and this file is that channel.
 *
 * ## Why a separate file rather than the existing metadata bridge
 *
 * `/tmp/adp-result-metadata.json` already carries Node→Python facts (session id,
 * cost/turns, the spend-cap stop). Abort deliberately does not ride in it, for
 * three reasons that are properties of abort specifically:
 *
 * **It must be atomic.** The metadata bridge is a read-merge-write, which has a
 * window where the file on disk is truncated or half-written. Python reads the
 * abort signal exactly once, during teardown, and a torn read there means an
 * aborted run silently records itself as `failed`. This file is written aside and
 * `rename`d into place, which is atomic within a filesystem, so a reader sees
 * either the whole previous state or the whole new one and never a fragment.
 *
 * **It must be bound to this run and this attempt.** The metadata bridge is
 * keyed by nothing: whatever is in `/tmp` is assumed to belong to the current
 * run. That assumption is fine for cost/turns and fatal for abort, because a
 * stale sentinel is not a harmless wrong number — it is an unrelated run
 * reporting itself deliberately stopped, with its queue message deleted and its
 * controls revoked. So the payload names the run and the control generation it
 * was written for, and the reader rejects anything that does not match.
 *
 * **Its failure mode must be one-directional.** A missing, malformed, truncated,
 * stale or future-versioned sentinel all mean *"no abort happened"* — never
 * "abort happened", and never a raised error. Losing an abort signal costs a
 * mislabelled outcome that an operator can see and re-issue; fabricating one
 * deletes a live run's queue message. Those are not symmetric, so the validation
 * below is deliberately strict and every rejection path returns the same
 * "no abort" answer.
 *
 * ## Why this is not in `agent-worker.ts`
 *
 * `agent-worker.ts` calls `main()` at module load and exports nothing, so a test
 * cannot import it — its sibling suites assert against its source *text*. The
 * sentinel's validation rules are exactly the part that needs real unit tests
 * (five distinct rejection cases), so they live here, in a module with no SDK
 * import and no side effects at load. This mirrors why #3961 split
 * `control-command-apply.ts` out of the same file.
 *
 * Imports are `fs` only, deliberately — the same leaf-module discipline as
 * `lib/tokenFile.ts`. A module the jest suites can import must not reach the SDK
 * transitively.
 */
import { closeSync, fsyncSync, openSync, renameSync, readFileSync, unlinkSync, writeSync } from 'fs';
import { randomUUID } from 'crypto';

/**
 * Schema version of the sentinel payload.
 *
 * Read as an exact match, not a minimum. A *newer* writer's payload is rejected
 * by an older reader rather than partially understood: the fields this reader
 * validates are the run binding and the generation, so "understand what I can and
 * ignore the rest" is precisely how a future field that narrows the abort's scope
 * would be silently dropped. The two halves ship in one image, so an exact match
 * is the normal state and a mismatch means something is genuinely wrong.
 */
export const ABORT_SENTINEL_VERSION = 1;

/** Default sentinel location. Mirrors the sibling `/tmp` bridges. */
export const ABORT_SENTINEL_PATH = '/tmp/adp-abort-sentinel.json';

/**
 * Bound on the reason text.
 *
 * The reason is operator-supplied and ends up in a GitHub comment, so it is
 * length-capped at the boundary rather than trusted. Matches the control
 * contract's `MAX_REASON_LENGTH` so a reason that survived the listener's
 * bounding is not re-bounded to a different size here.
 */
export const MAX_SENTINEL_REASON_LENGTH = 200;

/**
 * Bound on the stored envelope, matching `MAX_ENVELOPE_BYTES` in
 * `control-envelope.ts` and `_MAX_ENVELOPE_BYTES` in the Python verifier.
 *
 * A real envelope is well under this. The cap exists so that the one
 * caller-supplied field of unbounded length cannot be used to write an arbitrary
 * payload into the sentinel — which would then be refused by the reader's own
 * byte ceiling, turning a forged envelope into a *lost* abort.
 */
export const MAX_SENTINEL_ENVELOPE_LENGTH = 8192;

/**
 * Bound on the recorded signed request body, base64 — mirrors
 * `MAX_SIGNED_BODY_BYTES` in `lib/abort_sentinel.py`.
 *
 * The listener caps a control request body at `MAX_BODY_BYTES` (16 KiB) and base64
 * inflates by 4/3, so a legitimate value is always under this. The bytes are only
 * ever hashed and JSON-parsed, never executed, but they are attacker-influenced
 * input read during teardown and are bounded like every other field here.
 */
export const MAX_SIGNED_BODY_LENGTH = 4 * Math.ceil((16 * 1024) / 3) + 4;

/**
 * Byte ceiling on the whole sentinel file — mirrors `MAX_SENTINEL_BYTES` in
 * `lib/abort_sentinel.py`.
 *
 * Declared here so the limit has one definition per language rather than living
 * only in the reader that enforces it. **Bytes, not characters**: the Python half
 * enforces it on a binary read, because a text-mode character count admits up to
 * four times the intended size in multibyte UTF-8. Anything measuring this limit
 * must use `Buffer.byteLength`, not `String.length`.
 *
 * Derived from the two unbounded-by-nature fields rather than written as a round
 * number, because a round number was wrong. This was a flat 8 KiB while the
 * document had to hold a base64 signed body of up to {@link
 * MAX_SIGNED_BODY_LENGTH} (21852) plus an envelope of up to {@link
 * MAX_SENTINEL_ENVELOPE_LENGTH} — so an abort request the listener fully accepts
 * (`MAX_BODY_BYTES` is 16 KiB, and JSON whitespace alone gets a two-field body
 * there) produced a document the writer stored and the reader then refused on
 * size. The operator was told the abort was recorded, because the writer returned
 * `true`, and the finalizer classified the run by exit code — reporting a
 * deliberate stop as a crash, which is the single outcome this story exists to
 * make impossible.
 *
 * The 2 KiB tail covers the remaining short fields and JSON punctuation: the
 * version, the run id, the generation, a UUID command id, an ISO timestamp, the
 * delivery literal and the keys. It is slack, not a budget — nothing is sized to
 * consume it.
 */
export const MAX_SENTINEL_BYTES = MAX_SIGNED_BODY_LENGTH + MAX_SENTINEL_ENVELOPE_LENGTH + 2048;

/** The persisted payload. Carries no credential and no native session handle. */
export interface AbortSentinel {
  version: number;
  /** The run this abort belongs to — `ADP_CONTROL_RUN_ID`. */
  run_id: string;
  /** The control generation that accepted the abort. */
  generation: number;
  /** Journal id of the abort command, for correlating evidence. */
  command_id: string;
  /** When the abort was requested, ISO-8601. */
  requested_at: string;
  /**
   * The exact request body bytes the gateway signed, base64 — Issue #3963.
   *
   * **This is where the operator's reason lives, and the only place it lives.**
   * An earlier revision of this payload also carried a plain `reason` field. That
   * field was covered by no signature — it sat beside the envelope in a file the
   * agent's own `Bash` tool can write — and a real-signature reproduction minted a
   * valid envelope for one reason, wrote different text into that field, and had
   * the substitution attributed to the human who authorized the abort. It is gone
   * rather than merely unused: a field named `reason` in this document would be
   * read as the reason by the next person who needs one, which is exactly how the
   * gap arose.
   *
   * The envelope commits to `sha256` of the operator's HTTP request body, but the
   * supervising process never saw that body, so it held a digest with nothing to
   * compare against. This field supplies the preimage: the finalizer hashes these
   * bytes, requires the result to equal the signed `body_digest`, and only then
   * reads the `reason` out of them. That turns the reason from an unsigned
   * assertion into part of what the gateway's signature covers.
   *
   * These are bytes off the socket, recorded verbatim. They are never re-encoded
   * or re-serialized: a digest is over exact bytes, and `JSON.parse` followed by
   * `JSON.stringify` is not a byte-preserving round trip (key order, whitespace
   * and number formatting all move), so a re-serialized body would fail the very
   * check it exists to pass.
   */
  signed_body_base64: string;
  /**
   * Whether the live authority recheck *accepted* this command — Issue #3963.
   *
   * A signed envelope proves the gateway issued an authorization. It does not
   * prove the authorization still held at the moment of use: `deliverAuthorized`
   * re-asks the gateway immediately before the executor runs and settles the
   * command `rejected` when the grant was revoked, the revocation epoch moved, or
   * the operator's membership lapsed. A command refused there leaves its valid
   * envelope behind, and a finalizer that read only the envelope would honour an
   * abort the platform had explicitly just denied.
   *
   * Only `'accepted'` is written, and only from the executor — which by
   * construction runs only after that recheck returned true. The reader requires
   * this exact value, so an older writer's document or a fabricated one is refused
   * rather than being treated as authorized.
   */
  delivery: 'accepted';
  /**
   * The gateway's signed authorization for this abort — Issue #3963.
   *
   * The sentinel's own fields are all self-asserted: any code running in this pod
   * can write a file claiming this run was aborted, and the agent's own tool
   * surface includes `Bash`. So the file alone cannot establish that an operator
   * authorized anything, and a reader that trusted it would delete a live run's
   * queue message on the strength of a document the run wrote about itself.
   *
   * This field is the answer. It is the Ed25519 envelope the gateway minted for
   * this exact command — bound to this run, this generation, the `abort` action,
   * this `command_id` and the request body's digest — and the signing key exists
   * only in the gateway. The worker image holds public verification keys and has
   * no signing path, so this token is the one artifact here that cannot be forged
   * from inside the pod.
   *
   * Optional in the type, because the envelope is only present when the verb
   * required one. The reader decides what an absent envelope means; it does not
   * get to assume authorization.
   */
  envelope?: string | null;
}

/** What the reader needs to prove a sentinel belongs to the current run. */
export interface AbortSentinelBinding {
  runId: string;
  generation: number;
}

/**
 * Write the sentinel atomically.
 *
 * Returns `true` only when the payload is durably in place under its final name.
 * The caller must not report a successful abort on `false`: a sentinel that did
 * not land means the Python half will classify this run by its exit code, so
 * claiming an abort would be claiming an outcome that was never recorded.
 *
 * Atomicity is `write to a unique temp name in the same directory, then rename`
 * — the `lib/tokenFile.ts` pattern. Same directory matters: `rename` is only
 * atomic within a filesystem, and `/tmp` may be a different mount from anywhere
 * else. The temp name carries a UUID rather than the pid, because a pid is
 * reusable and `wx` would then fail against a stale leftover.
 *
 * The content is `fsync`ed before the rename, matching
 * `lib/control_renewal.py::_write` for the same document family. Buffered-and-
 * renamed is atomic with respect to *readers* but not with respect to the pod
 * dying, and the reader here runs after this process has exited — precisely the
 * window where an unflushed write is lost.
 */
export function writeAbortSentinel(
  input: {
    binding: AbortSentinelBinding;
    commandId: string;
    requestedAt?: string;
    /** The gateway envelope that authorized this abort; see {@link AbortSentinel.envelope}. */
    envelope?: string | null;
    /**
     * The exact signed request body, base64; see
     * {@link AbortSentinel.signed_body_base64}. Without it the finalizer cannot
     * bind the reason to the signature, so a sentinel is not written at all.
     */
    signedBodyBase64?: string | null;
  },
  options: { sentinelPath?: string; log?: (level: string, message: string) => void } = {},
): boolean {
  const sentinelPath = options.sentinelPath ?? ABORT_SENTINEL_PATH;
  const log = options.log ?? (() => {});

  const payload: AbortSentinel = {
    version: ABORT_SENTINEL_VERSION,
    run_id: input.binding.runId,
    generation: input.binding.generation,
    command_id: input.commandId,
    requested_at: input.requestedAt ?? new Date().toISOString(),
    // Verbatim; see the field docs. Bounded only in length.
    signed_body_base64: typeof input.signedBodyBase64 === 'string' ? input.signedBodyBase64 : '',
    // Written unconditionally because this function is reached only from the abort
    // executor, which runs only after `deliverAuthorized` re-checked the grant
    // against the live gateway. There is no code path that records an abort whose
    // delivery was refused: that path settles `rejected` and never reaches here.
    delivery: 'accepted',
    // Copied verbatim — this is a signature over exact bytes, so any
    // normalization here would invalidate it. Bounded only in length, since an
    // over-long value cannot be a real envelope and must not be written to disk.
    envelope: typeof input.envelope === 'string' && input.envelope.length > 0
      && input.envelope.length <= MAX_SENTINEL_ENVELOPE_LENGTH
      ? input.envelope
      : null,
  };

  // An unbound sentinel is worse than none: the reader's run/generation check is
  // the only thing standing between a leftover file and an unrelated run
  // reporting itself aborted, and a blank binding can never satisfy it. Refusing
  // to write is the honest outcome — the abort still stops the run, it just does
  // not get to claim the aborted outcome.
  if (!payload.run_id || !Number.isInteger(payload.generation) || payload.generation < 1
    || !Number.isSafeInteger(payload.generation)) {
    log('ERROR', 'abort sentinel not written: run identity or generation is missing');
    return false;
  }

  // A sentinel with no signed body cannot have its reason bound to the gateway's
  // signature, and the reader refuses it. Refusing to write is the honest
  // equivalent: the abort still stops the run — `adapter.cancel()` is
  // unconditional and runs regardless — it simply will not be *reported* as an
  // abort, and the journal says so. Writing a document the reader is guaranteed to
  // reject would produce the same outcome while looking like it had succeeded.
  if (!payload.signed_body_base64) {
    log('ERROR', 'abort sentinel not written: the signed request body was not available');
    return false;
  }

  // Serialize once, then refuse anything this document's own reader would refuse.
  //
  // The writer's return value is what the journal reports to the operator: `true`
  // settles the abort command `applied` — "recorded for finalization" — and
  // `false` settles it `unknown`. So a document that lands on disk but fails
  // validation is the worst of the three outcomes: the operator is told the abort
  // was recorded while the finalizer, finding nothing it can honour, classifies the
  // run by exit code and reports a deliberate stop as a crash.
  //
  // That was reachable, not hypothetical. The byte ceiling was a flat 8 KiB while
  // the signed body alone may be 21852 base64 characters, so an abort request the
  // listener fully accepts produced a stored-and-then-rejected document. The
  // ceiling is now derived from the fields, which fixes that instance; this check
  // closes the class. Validating the real serialized bytes — the same `JSON.stringify`
  // output that is about to be written, measured with `Buffer.byteLength` — means
  // any future field, or any future divergence between the two limits, surfaces
  // here as an honest `false` rather than as a mislabelled run.
  const serialized = JSON.stringify(payload);
  const serializedBytes = Buffer.byteLength(serialized, 'utf8');
  if (serializedBytes > MAX_SENTINEL_BYTES) {
    log('ERROR', `abort sentinel not written: ${serializedBytes} bytes exceeds the ${MAX_SENTINEL_BYTES}-byte ceiling`);
    return false;
  }
  if (!validateAbortSentinel(payload, input.binding)) {
    // Belt-and-braces against the writer and reader drifting apart on any rule,
    // not just size. Cheap (one in-memory validation per abort, and there is at
    // most one abort per run) and it converts a silent disagreement into a logged
    // refusal at the moment the record is made.
    log('ERROR', 'abort sentinel not written: the payload would not pass validation');
    return false;
  }

  const tempPath = `${sentinelPath}.${randomUUID()}.tmp`;
  try {
    // 'wx' fails rather than truncating if the name somehow exists, so a
    // colliding write is an error instead of two writers interleaving.
    const handle = openSync(tempPath, 'wx', 0o600);
    try {
      // The exact bytes that were measured and validated above, not a second
      // `JSON.stringify` of the same object — so what lands on disk is what passed.
      writeSync(handle, serialized);
      fsyncSync(handle);
    } finally {
      closeSync(handle);
    }
    renameSync(tempPath, sentinelPath);
    return true;
  } catch (err) {
    log('ERROR', `abort sentinel write failed: ${(err as Error).message}`);
    return false;
  } finally {
    // The temp name is never read, so cleanup failure is uninteresting. After a
    // successful rename there is nothing at this path and the unlink is a no-op.
    try { unlinkSync(tempPath); } catch { /* renamed, or never created */ }
  }
}

/**
 * Read and validate the sentinel.
 *
 * Returns the payload only when it is well-formed, version-exact and bound to
 * the supplied run and generation. Every other case — absent, unreadable,
 * unparseable, wrong shape, wrong version, different run, superseded generation
 * — returns `null`, which callers must treat as "this run was not aborted".
 *
 * Generation is compared for *equality*, not "at least". A sentinel from a
 * superseded generation is a stale file from a previous control registration,
 * and honouring it would let an abort aimed at an attempt that has already ended
 * finalize the attempt that replaced it.
 */
export function readAbortSentinel(
  binding: AbortSentinelBinding,
  options: { sentinelPath?: string } = {},
): AbortSentinel | null {
  const sentinelPath = options.sentinelPath ?? ABORT_SENTINEL_PATH;
  let parsed: unknown;
  try {
    // Read as bytes and bound on bytes before decoding, the same contract the
    // Python reader enforces. `readFileSync(path, 'utf8')` would decode the whole
    // file first, so an oversized document would already be in memory as a string
    // by the time any limit could be applied — and a limit applied to that string
    // would count characters, which is a different and looser bound than the
    // declared byte ceiling.
    const bytes = readFileSync(sentinelPath);
    if (bytes.byteLength > MAX_SENTINEL_BYTES) return null;
    parsed = JSON.parse(bytes.toString('utf8'));
  } catch {
    // Absent is the overwhelmingly common case (no abort was requested) and is
    // not worth distinguishing from corrupt here: both mean "no abort".
    return null;
  }
  return validateAbortSentinel(parsed, binding);
}

/**
 * The validation rules, separated from the I/O so they can be tested directly
 * and so the Python reader has one unambiguous specification to mirror.
 */
export function validateAbortSentinel(
  parsed: unknown,
  binding: AbortSentinelBinding,
): AbortSentinel | null {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const candidate = parsed as Record<string, unknown>;

  if (candidate.version !== ABORT_SENTINEL_VERSION) return null;

  const runId = candidate.run_id;
  const generation = candidate.generation;
  const commandId = candidate.command_id;
  const requestedAt = candidate.requested_at;

  if (typeof runId !== 'string' || !runId) return null;
  // `Number.isSafeInteger`, not `isInteger`: above 2^53 a double no longer
  // represents consecutive integers, so two different generations can compare
  // equal. Python's arbitrary-precision `int` has no such ceiling, which made this
  // a cross-language divergence in the direction that matters — the Python half is
  // the one that deletes the queue message. Both halves now refuse the same values.
  if (typeof generation !== 'number' || !Number.isSafeInteger(generation)) return null;
  if (typeof commandId !== 'string' || !commandId) return null;
  if (typeof requestedAt !== 'string' || !requestedAt) return null;

  // The run binding. Both must match: the run id alone would accept a sentinel
  // from a superseded attempt of the same run, and the generation alone would
  // accept one from a different run that happened to share a generation number.
  if (runId !== binding.runId) return null;
  if (generation !== binding.generation) return null;

  // The live delivery outcome and the signed bytes. Both are required, on the same
  // rule the Python reader applies: a document lacking either cannot establish that
  // the abort was accepted at the moment of use, nor that its reason is the
  // operator's. There is no legitimate writer that omits them.
  if (candidate.delivery !== 'accepted') return null;
  const signedBody = candidate.signed_body_base64;
  if (typeof signedBody !== 'string' || !signedBody
    || signedBody.length > MAX_SIGNED_BODY_LENGTH) return null;

  const envelope = candidate.envelope;
  return {
    version: ABORT_SENTINEL_VERSION,
    run_id: runId,
    generation,
    command_id: commandId,
    requested_at: requestedAt,
    // A `reason` key on the document is neither read nor carried forward. See
    // {@link AbortSentinel.signed_body_base64}: the reason is derived from the
    // signed bytes, and re-exposing an unsigned field of the same name here would
    // put the forgeable value back within reach of the next caller who needs one.
    signed_body_base64: signedBody,
    delivery: 'accepted',
    // Surfaced, never judged here. This side cannot verify a signature it has no
    // business verifying: the envelope is checked by whoever *acts* on the
    // sentinel, which is the Python finalizer. A malformed or absent value
    // becomes `null` so that "no proof" is a single, unambiguous state rather
    // than a shape the consumer has to re-test.
    //
    // Deliberately not a reason to reject the whole document. The envelope
    // governs whether the abort is *authorized*, not whether the file parsed, and
    // conflating the two would make an unauthorized sentinel indistinguishable
    // from a corrupt one to the caller that must tell them apart.
    envelope: typeof envelope === 'string' && envelope.length > 0
      && envelope.length <= MAX_SENTINEL_ENVELOPE_LENGTH
      ? envelope
      : null,
  };
}

/**
 * Collapse whitespace and truncate. `null`/empty become `null`, not `""`.
 *
 * Nothing on this runtime's run path calls it any more: the reason is derived
 * from the signed body by the finalizing Python half, which applies its own
 * `bound_sentinel_reason`. It is kept, and kept exported, because it is the
 * TypeScript half of a rule both runtimes are required to implement identically —
 * the shared `reason_vectors` table is evaluated against *this* function here and
 * against Python's there. Deleting it would leave that rule pinned on one side
 * only, and the bounding rule is what stops signed-but-hostile operator text from
 * forging the layout of the comment it is interpolated into. A signature proves
 * who wrote the text, not that the text is safe to print.
 *
 * If a future control verb needs to bound operator text on this side — a pause
 * annotation, say — this is the function to use, not a second local copy.
 */
export function boundSentinelReason(reason: string | null | undefined): string | null {
  if (typeof reason !== 'string') return null;
  const collapsed = reason.replace(/\s+/g, ' ').trim();
  if (!collapsed) return null;
  return collapsed.length <= MAX_SENTINEL_REASON_LENGTH
    ? collapsed
    : `${collapsed.slice(0, MAX_SENTINEL_REASON_LENGTH - 1)}…`;
}

/**
 * Resolve the run binding from the process environment.
 *
 * These are the same variables the control listener is configured from, placed
 * there by `entrypoint.py` only when the control flag is on and registration
 * succeeded — so a run with no control channel yields no binding, and
 * `writeAbortSentinel` refuses rather than writing an unbindable file.
 *
 * Returns `null` rather than a partial binding: a binding with a blank run id
 * cannot be validated against, and inventing a default here would defeat the
 * staleness check that is the sentinel's whole safety property.
 */
export function abortSentinelBindingFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): AbortSentinelBinding | null {
  const runId = (env.ADP_CONTROL_RUN_ID || '').trim();
  const generation = parseStrictGeneration(env.ADP_CONTROL_GENERATION);
  if (!runId || generation === null) return null;
  return { runId, generation };
}

/**
 * Parse a generation from its environment string, rejecting trailing garbage.
 *
 * `Number.parseInt` stops at the first non-digit and returns what it got, so
 * `'1junk'` yields `1` — it would bind this run to generation 1 on input that
 * means nothing of the kind, while the Python side's `int()` raises on the same
 * bytes. The binding is the sentinel's entire staleness defence, so the two
 * halves have to agree about what a generation *is*: a bare run of decimal
 * digits, nothing else.
 */
export function parseStrictGeneration(raw: string | undefined | null): number | null {
  const text = (raw ?? '').trim();
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < 1) return null;
  return value;
}

/**
 * Remove a sentinel. Used by tests and by nothing on the run path.
 *
 * Deliberately not called during teardown: the sentinel must survive until the
 * Python half has read it, and that read happens after this process has exited.
 * `/tmp` is pod-local and the pod is torn down with the run, so there is no
 * lifetime to manage beyond the process.
 */
export function clearAbortSentinel(options: { sentinelPath?: string } = {}): void {
  try { unlinkSync(options.sentinelPath ?? ABORT_SENTINEL_PATH); }
  catch { /* already absent */ }
}
