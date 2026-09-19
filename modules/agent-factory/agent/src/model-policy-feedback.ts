/** Requester-visible report-only feedback for persona-model resolution (#2293). */

export interface ModelPolicyFeedback {
  marker: string;
  body: string;
}

/**
 * A fetched issue comment as the dedup check needs to see it.
 *
 * ``viewerDidAuthor`` is GitHub's own answer to "did the credential making this
 * request write this comment?". It is computed server-side against our
 * installation token, so a commenter cannot set it and the worker needs no
 * extra signing secret to trust it. It is optional here only because the field
 * may be absent from an older/partial payload -- see ``suppressedBy``.
 */
export interface FeedbackComment {
  body: string;
  viewerDidAuthor?: boolean;
}

type Environment = Record<string, string | undefined>;

/**
 * Requester-actionable refusal reasons. A closed vocabulary: anything outside
 * it is NOT rendered, so server-side error text can never reach the requester.
 */
const REQUESTER_ACTIONS: Record<string, string> = {
  direct_override_unresolved:
    'That model name is not in the published Agent Models catalogue. Check the catalogue for a published alias and request that instead.',
  model_unavailable:
    'That model is not currently active in the Agent Models catalogue. Choose an active model.',
  harness_incompatible:
    'That model is not compatible with this persona’s agent harness. Choose a model this persona supports.',
  persona_incompatible:
    'That model is not available for this persona. Choose a model this persona supports.',
  not_permitted:
    'Your organization’s model policy does not permit that model. Ask an ADP administrator to review the policy.',
  class_default_unavailable:
    'No default model is configured for this persona class. Ask an ADP administrator to configure one.',
};

/**
 * Reasons that describe an internal policy/authority condition rather than
 * anything the requester chose. They are named (they are stable, non-secret
 * codes and they help an operator correlate) but the guidance points at an
 * operator, because there is no requester-side action.
 */
const OPERATOR_REASONS: ReadonlySet<string> = new Set([
  'authority_expired',
  'authority_kind_unsupported',
  'authority_unavailable',
  'dispatch_not_pending',
  'dispatch_unresolved',
  'evidence_stale',
  'model_validation_mismatch',
  'not_invocable',
  'parent_snapshot_missing',
  'principal_unavailable',
  'probing_disabled',
  'runtime_posture_unsupported',
  'service_principal_unregistered',
  'snapshot_altered',
  'snapshot_audience_mismatch',
  'snapshot_binding_missing',
  'snapshot_cache_disabled',
  'snapshot_cache_missing',
  'snapshot_cache_owner_mismatch',
  'snapshot_cache_owner_unproven',
  'snapshot_cache_stale',
  'snapshot_chain_mismatch',
  'snapshot_cross_tenant',
  'snapshot_expired',
  'snapshot_malformed',
  'snapshot_missing',
  'snapshot_persistence_failed',
  'snapshot_revision_mismatch',
  'snapshot_too_large',
  'snapshot_unsupported_revision',
  'unverified_provenance',
]);

const OPERATOR_ACTION =
  'ADP could not establish the model policy for this run. Ask an ADP operator to check model-policy evidence for this run.';

const UNKNOWN_REASON_ACTION =
  'ADP could not admit the request for an unrecognized reason. Ask an ADP operator to check model-policy evidence for this run.';

/** Placeholder shown instead of an unrenderable untrusted value. */
const UNRENDERABLE = 'unavailable';

/**
 * Reduce an untrusted value to characters that cannot carry Markdown, HTML,
 * links or mentions.
 *
 * This is an allowlist, not a blocklist: only characters that legitimately
 * occur in a model id, a reason code or a run id survive
 * (``us.anthropic.claude-opus-4-6-v1``, ``direct_override_unresolved``, a
 * UUID). Everything else -- backtick, ``@``, ``<``, ``>``, ``[``, ``]``,
 * ``(``, ``)``, ``*``, ``_``-adjacent emphasis pairs aside, whitespace and all
 * control characters -- is dropped rather than escaped, so there is no
 * escaping scheme to get wrong and no encoding trick to slip past. Because a
 * backtick cannot survive, interpolating the result inside a code span is
 * safe: the span cannot be closed early.
 */
export function sanitizeUntrusted(value: string | undefined, limit = 128): string {
  if (!value) return UNRENDERABLE;
  const cleaned = value.replace(/[^A-Za-z0-9._:-]/g, '');
  if (!cleaned) return UNRENDERABLE;
  return cleaned.length > limit ? `${cleaned.slice(0, limit)}...` : cleaned;
}

/** The postures the platform defines. Anything else is treated as unknown. */
type Posture = 'disabled' | 'report_only' | 'enforcing';

function knownPosture(value: string | undefined): Posture | null {
  return value === 'disabled' || value === 'report_only' || value === 'enforcing' ? value : null;
}

/**
 * Describe what actually happened to this run's model, truthfully.
 *
 * Only ``enforcing`` may claim the request was refused before inference,
 * because only there does a refusal actually stop the run. In ``report_only``
 * and ``disabled`` the legacy assignment still executes, and an unknown
 * posture makes no claim either way -- the process is demonstrably still
 * running, so asserting that inference was blocked would be false.
 */
function behaviorFor(posture: Posture | null): string {
  switch (posture) {
    case 'report_only':
      return 'ADP is in report-only mode for model policy, so this run continued on its existing model assignment. Nothing was substituted.';
    case 'disabled':
      return 'Model policy is disabled, so this run continued on its existing model assignment. Nothing was substituted.';
    case 'enforcing':
      return 'Model policy is enforcing, so the request was refused before agent inference.';
    default:
      return 'ADP could not confirm the model-policy posture for this run, so this notice makes no claim about whether inference was blocked.';
  }
}

function actionFor(reason: string): string {
  if (REQUESTER_ACTIONS[reason]) return REQUESTER_ACTIONS[reason];
  if (OPERATOR_REASONS.has(reason)) return OPERATOR_ACTION;
  return UNKNOWN_REASON_ACTION;
}

export function buildModelPolicyFeedback(
  env: Environment = process.env,
): ModelPolicyFeedback | null {
  const requested = env.ADP_MODEL_REQUESTED;
  const resolved = env.ADP_MODEL_RESOLVED;
  // PMM-06's merged decision contract reports a refusal as
  // ``status=unavailable`` plus a stable ``reason``; a successfully resolved
  // proposal is ``status=proposed``. There is no separate admission field to
  // consult, so the refusal signal is the status itself.
  const policyStatus = env.ADP_MODEL_POLICY_STATUS;

  // Feedback is requester-facing and only for a directive that did not stand.
  // Absent a directive there is nothing to answer for, and an accepted
  // proposal needs no warning: ordinary mapping shadow telemetry stays
  // operator-facing so report-only does not comment on every healthy run.
  if (!requested) return null;
  if (policyStatus !== 'unavailable' && resolved) return null;

  const rawReason = env.ADP_MODEL_POLICY_REASON;
  // Only a reason drawn from the closed vocabulary is ever rendered. An
  // unrecognized or hostile value is reported as ``unrecognized`` so arbitrary
  // server text cannot be reflected to the requester.
  const reasonKey = rawReason ? sanitizeUntrusted(rawReason, 64) : '';
  const known = Boolean(REQUESTER_ACTIONS[reasonKey]) || OPERATOR_REASONS.has(reasonKey);
  const displayReason = known ? reasonKey : 'unrecognized';

  const safeRequested = sanitizeUntrusted(requested);
  const messageId = sanitizeUntrusted(env.ADP_MESSAGE_ID, 64);
  const posture = knownPosture(env.ADP_MODEL_POLICY_POSTURE);

  const marker = `<!-- adp-model-policy-feedback:${messageId} -->`;
  return {
    marker,
    body:
      `${marker}\n⚠️ Model request \`${safeRequested}\` could not be admitted ` +
      `(\`${displayReason}\`). ${actionFor(reasonKey)} ${behaviorFor(posture)}`,
  };
}

/**
 * Which earlier comment, if any, suppresses this feedback.
 *
 * The marker alone is not enough: any account able to comment could post it
 * and silence a truthful warning permanently. A comment suppresses only when
 * GitHub also confirms that *our own* credential authored it, which is what
 * makes a genuine retry of this run idempotent while a forged marker from a
 * human or another bot is ignored.
 *
 * When the authorship signal is absent we deliberately do NOT suppress:
 * posting a duplicate warning is recoverable, losing the warning is not.
 */
export function suppressedBy(
  comments: ReadonlyArray<FeedbackComment>,
  feedback: ModelPolicyFeedback,
): { suppressed: boolean; forgedMarkerSeen: boolean; authorshipUnknown: boolean } {
  let forgedMarkerSeen = false;
  let authorshipUnknown = false;
  for (const comment of comments) {
    if (!comment.body?.includes(feedback.marker)) continue;
    if (comment.viewerDidAuthor === true) {
      return { suppressed: true, forgedMarkerSeen, authorshipUnknown };
    }
    if (comment.viewerDidAuthor === false) forgedMarkerSeen = true;
    else authorshipUnknown = true;
  }
  return { suppressed: false, forgedMarkerSeen, authorshipUnknown };
}

export type FeedbackOutcome = 'posted' | 'suppressed' | 'not_applicable';

/**
 * Decide and deliver requester feedback on the real posting path.
 *
 * This owns the whole orchestration -- build, authenticate the dedup evidence,
 * post -- so that the posting decision is covered by tests rather than only
 * the message text. ``postComment`` is the worker's real comment poster; a
 * failure to deliver is logged and never fails the run, because this is a
 * notice about a model request, not the run's work.
 */
export async function deliverModelPolicyFeedback(deps: {
  comments: ReadonlyArray<FeedbackComment>;
  postComment: (body: string) => Promise<void>;
  log?: (level: string, message: string) => void;
  env?: Environment;
}): Promise<FeedbackOutcome> {
  const feedback = buildModelPolicyFeedback(deps.env ?? process.env);
  if (!feedback) return 'not_applicable';

  const { suppressed, forgedMarkerSeen, authorshipUnknown } = suppressedBy(
    deps.comments,
    feedback,
  );
  if (suppressed) return 'suppressed';
  if (forgedMarkerSeen) {
    deps.log?.(
      'WARN',
      'Model policy feedback: marker present on a comment ADP did not author; posting anyway.',
    );
  }
  if (authorshipUnknown) {
    deps.log?.(
      'WARN',
      'Model policy feedback: comment authorship unverifiable; posting rather than suppressing.',
    );
  }

  try {
    await deps.postComment(feedback.body);
    return 'posted';
  } catch (err) {
    deps.log?.('WARN', `Model policy feedback post failed: ${(err as Error).message}`);
    return 'not_applicable';
  }
}
