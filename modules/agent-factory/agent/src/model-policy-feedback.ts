/** Requester-visible report-only feedback for persona-model resolution (#2293). */

export interface ModelPolicyFeedback {
  marker: string;
  body: string;
}

type Environment = Record<string, string | undefined>;

const ACTIONS: Record<string, string> = {
  direct_override_unresolved:
    'Use `adp models catalog --persona <persona>` to choose a published alias.',
  model_unavailable:
    'Choose an active model from the Agent Models catalogue.',
  harness_incompatible:
    'Choose a model compatible with this persona’s agent harness.',
  not_permitted:
    'Ask an ADP administrator to review the organization model policy.',
  not_invocable:
    'Ask an ADP operator to verify model access at the resolved destination.',
  evidence_stale:
    'Ask an ADP operator to refresh destination invocability evidence.',
  probing_disabled:
    'Ask an ADP operator to establish destination invocability evidence.',
};

function bounded(value: string | undefined, fallback: string): string {
  if (!value || value.length > 256 || /[\x00-\x1f\x7f]/.test(value)) return fallback;
  return value;
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
  const reason = bounded(env.ADP_MODEL_POLICY_REASON, 'model_policy_unavailable');

  // Feedback is requester-facing and only for a directive that did not stand.
  // Absent a directive there is nothing to answer for, and an accepted
  // proposal needs no warning: ordinary mapping shadow telemetry stays
  // operator-facing so report-only does not comment on every healthy run.
  if (!requested) return null;
  if (policyStatus !== 'unavailable' && resolved) return null;

  const safeRequested = bounded(requested, '<invalid value>');
  const messageId = bounded(env.ADP_MESSAGE_ID, 'unknown-run');
  const posture = bounded(env.ADP_MODEL_POLICY_POSTURE, 'report_only');
  const action = ACTIONS[reason] ?? 'Review the Agent Models catalogue or ask an ADP operator for help.';
  const behavior = posture === 'report_only'
    ? 'ADP is currently in report-only mode, so the existing legacy model assignment is unchanged.'
    : 'The model request was refused before agent inference.';
  const marker = `<!-- adp-model-policy-feedback:${messageId} -->`;
  return {
    marker,
    body: `${marker}\n⚠️ Model request \`${safeRequested}\` could not be admitted (` +
      `\`${reason}\`). ${action} ${behavior}`,
  };
}

export function feedbackAlreadyPosted(
  comments: ReadonlyArray<{ body: string }>,
  feedback: ModelPolicyFeedback,
): boolean {
  return comments.some(comment => comment.body.includes(feedback.marker));
}
