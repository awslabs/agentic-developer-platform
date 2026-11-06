import { ChatDataClient, ChatDataError, TURN_ERROR_MESSAGE_MAX_LENGTH, type ModelPolicyRefusal } from './gateway/chat-data-client';

/**
 * A refused or unavailable model-policy decision ends the turn visibly (#7968).
 *
 * The gateway answers `POST /v1/chat/model/decision` with a structured 503 naming the
 * policy reason (`probing_disabled`, `evidence_stale`, `not_permitted`, ...). Left alone,
 * the sandbox exited without a result, `last_response` stayed empty and the owner kept a
 * working indicator forever. Here the sandbox publishes a terminal failed result through
 * the same channel a normal reply uses (`turn/result`, whose terminal content becomes the
 * owner's `last_response`), then exits non-zero so the supervisor reconciles the pod.
 *
 * A decision the gateway issued under the `advisory` or `off` chat posture (#7997) is a
 * decision, not a refusal: the turn runs on the resolved model. It is noted on stderr so the
 * pod log says the turn ran on advice (the signed body and the gateway journal record it).
 */
export type TurnModel = Awaited<ReturnType<ChatDataClient['modelDecision']>>;

/** One-line pod-log note for a decision issued under a relaxed chat posture; undefined when enforcing. */
export function modelDecisionPostureNote(model: TurnModel): string | undefined {
  if (model.posture === 'enforcing') return undefined;
  const evidence = model.postureVerified ? 'verified' : `unverified${model.rejectionReason ? ` (${model.rejectionReason})` : ''}`;
  return `Model policy posture ${model.posture}: running ${model.modelId} on advice, evidence ${evidence}`;
}
export type ModelDecisionFailure = { reason: string; retryable: boolean };

/** Retries are bounded and apply only when the gateway says the failure is transient. */
export const MODEL_DECISION_RETRIES = 2;
export const MODEL_DECISION_RETRY_DELAY_MS = 2_000;

export class SandboxModelDecisionError extends Error {
  constructor(readonly failure: ModelDecisionFailure, readonly cause: ChatDataError) {
    super(modelDecisionFailureMessage(failure));
    this.name = 'SandboxModelDecisionError';
  }
}

export function modelDecisionFailure(error: ChatDataError): ModelDecisionFailure | undefined {
  const policy: ModelPolicyRefusal | undefined = error.policy;
  return policy ? { reason: policy.reason, retryable: policy.retryable } : undefined;
}

export function modelDecisionFailureMessage(failure: ModelDecisionFailure): string {
  const hint = failure.reason === 'probing_disabled' || failure.reason === 'evidence_stale'
    ? 'Ask the platform operator to enable model invocability probing.'
    : failure.reason === 'not_permitted'
      ? 'The selected model is not permitted for this persona.'
      : failure.retryable
        ? 'Please retry in a moment.'
        : 'Ask the platform operator to check the model policy for this persona.';
  const message = `The assistant cannot run this turn: the model policy decision was unavailable (${failure.reason}). ${hint}`;
  return message.length <= TURN_ERROR_MESSAGE_MAX_LENGTH ? message : message.slice(0, TURN_ERROR_MESSAGE_MAX_LENGTH);
}

/**
 * Record the failed result that names the reason. A turn must be accepted before a result
 * can be recorded, so a failure before `nextTurn` accepts the owner's turn first. A gateway
 * that predates `error_message` rejects the field; the plain failed result still ends the turn.
 */
export async function publishModelDecisionFailure(
  client: ChatDataClient, failure: ModelDecisionFailure, options: { turnAccepted: boolean },
): Promise<void> {
  if (!options.turnAccepted) await client.nextTurn();
  try {
    await client.submitTurnResult({ outcome: 'failed', error_message: modelDecisionFailureMessage(failure) });
  } catch (error) {
    if (!(error instanceof ChatDataError && error.code === 'invalid_request')) throw error;
    await client.submitTurnResult({ outcome: 'failed' });
  }
}

export async function decideTurnModel(
  client: ChatDataClient,
  options: {
    turnAccepted: boolean;
    /** Request-scoped signal handed to each decision attempt. */
    signal?: AbortSignal;
    /** Lifecycle signal: an aborted sandbox stops retrying and publishes nothing. */
    abort?: AbortSignal;
    sleep?: (ms: number) => Promise<void>;
    /** Existing transient handling for a non-policy outage (for example the gateway catching up after admission). */
    retryTransient?: (error: ChatDataError) => Promise<boolean> | boolean;
    /** Where the advisory/off posture note goes; stderr by default. */
    warn?: (message: string) => void;
  },
): Promise<TurnModel> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  const warn = options.warn ?? ((message: string) => console.error(message));
  let policyRetries = 0;
  for (;;) {
    options.abort?.throwIfAborted();
    try {
      const model = await client.modelDecision(options.signal);
      const note = modelDecisionPostureNote(model);
      if (note) warn(note);
      return model;
    } catch (error) {
      options.abort?.throwIfAborted();
      if (!(error instanceof ChatDataError) || error.status === undefined) throw error;
      const failure = modelDecisionFailure(error);
      if (!failure) {
        if (await options.retryTransient?.(error)) continue;
        throw error;
      }
      if (failure.retryable && policyRetries < MODEL_DECISION_RETRIES) {
        policyRetries += 1;
        await sleep(MODEL_DECISION_RETRY_DELAY_MS);
        continue;
      }
      await publishModelDecisionFailure(client, failure, options).catch(() => undefined);
      throw new SandboxModelDecisionError(failure, error);
    }
  }
}
