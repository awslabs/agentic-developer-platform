import {
  buildModelPolicyFeedback,
  feedbackAlreadyPosted,
} from './model-policy-feedback';

describe('persona-model requester feedback', () => {
  it('renders an actionable, idempotent report-only direct-override refusal', () => {
    const feedback = buildModelPolicyFeedback({
      ADP_MESSAGE_ID: 'run-1',
      ADP_MODEL_REQUESTED: 'future-latest',
      ADP_MODEL_POLICY_POSTURE: 'report_only',
      ADP_MODEL_POLICY_STATUS: 'unavailable',
      ADP_MODEL_POLICY_REASON: 'direct_override_unresolved',
    });

    expect(feedback).not.toBeNull();
    expect(feedback?.body).toContain('adp models catalog');
    expect(feedback?.body).toContain('legacy model assignment is unchanged');
    expect(feedbackAlreadyPosted([], feedback!)).toBe(false);
    expect(feedbackAlreadyPosted([{ body: feedback!.body }], feedback!)).toBe(true);
  });

  it('warns when a resolved override is refused by live destination evidence', () => {
    // Live admission runs inside the gateway before it signs, so an
    // evidence refusal arrives as ``status=unavailable`` with the live
    // reason -- the edge never sees a "proposed but refused" decision.
    const feedback = buildModelPolicyFeedback({
      ADP_MESSAGE_ID: 'run-2',
      ADP_MODEL_REQUESTED: 'sonnet46',
      ADP_MODEL_RESOLVED: 'global.anthropic.claude-sonnet-4-6',
      ADP_MODEL_POLICY_POSTURE: 'report_only',
      ADP_MODEL_POLICY_STATUS: 'unavailable',
      ADP_MODEL_POLICY_REASON: 'evidence_stale',
    });

    expect(feedback?.body).toContain('refresh destination invocability evidence');
  });

  it('is silent for accepted or absent direct requests', () => {
    expect(buildModelPolicyFeedback({})).toBeNull();
    expect(buildModelPolicyFeedback({
      ADP_MODEL_REQUESTED: 'sonnet46',
      ADP_MODEL_RESOLVED: 'global.anthropic.claude-sonnet-4-6',
      ADP_MODEL_POLICY_STATUS: 'proposed',
    })).toBeNull();
  });

  it('bounds untrusted values before rendering them', () => {
    const feedback = buildModelPolicyFeedback({
      ADP_MESSAGE_ID: 'run-3',
      ADP_MODEL_REQUESTED: 'bad\nrequest',
      ADP_MODEL_POLICY_STATUS: 'unavailable',
      ADP_MODEL_POLICY_REASON: 'bad\nreason',
    });
    expect(feedback?.body).not.toContain('bad\n');
  });
});
