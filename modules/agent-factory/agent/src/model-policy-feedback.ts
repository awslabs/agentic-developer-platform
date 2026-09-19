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
  const admission = env.ADP_MODEL_POLICY_ADMISSION_STATUS;
  const policyStatus = env.ADP_MODEL_POLICY_STATUS;
  const reason = bounded(
    env.ADP_MODEL_POLICY_ADMISSION_REASON ?? env.ADP_MODEL_POLICY_REASON,
    'model_policy_unavailable',
  );

  // A valid direct override and an admitted proposed mapping need no warning.
  // Absence of a directive also needs no requester feedback; ordinary mapping
  // shadow telemetry remains operator-facing.
  if (!requested || (resolved && admission === 'admitted')) return null;
  if (resolved && admission !== 'refused' && policyStatus !== 'unavailable') return null;

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
