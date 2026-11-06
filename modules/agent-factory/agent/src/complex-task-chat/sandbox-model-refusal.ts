import { ChatDataClient, ChatDataError, TURN_ERROR_MESSAGE_MAX_LENGTH, type ModelInvokeRefusal } from './gateway/chat-data-client';

/**
 * A model invocation the gateway ended without a confirmed receipt names its reason (#8048).
 *
 * `POST /v1/chat/model/invoke` answers 200 with a `rejected` receipt when the call was refused
 * before it reached the provider (budget, quota, claim, readiness) and an `unknown` receipt
 * when the provider outcome was lost after it was sent. Left alone, the sandbox submitted
 * `{ outcome: 'failed' }` and the owner read only "The assistant could not complete this
 * turn." Here the receipt's `error_code` and bounded `error_detail` become the owner-visible
 * `error_message`, through the same channel a refused model-policy decision already uses
 * (`sandbox-model-decision.ts`, #7968). The text is bounded like any `TurnResult.error_message`
 * and never carries the request, a token or an identity: the gateway only forwards the class
 * and message of its own exception.
 */
export function modelInvokeRefusal(error: unknown): ModelInvokeRefusal | undefined {
  return error instanceof ChatDataError ? error.refusal : undefined;
}

const DETAIL_MAX_LENGTH = 300;

function ownerSafe(text: string | undefined): string {
  // Control characters would fail the gateway's error_message pattern; collapse them.
  return (text ?? '').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, ' ').trim().slice(0, DETAIL_MAX_LENGTH);
}

export function modelInvokeFailureMessage(refusal: ModelInvokeRefusal): string {
  const code = refusal.error_code ?? (refusal.status === 'rejected' ? 'model_admission_refused' : 'model_outcome_unknown');
  const detail = ownerSafe(refusal.error_detail) || ownerSafe(refusal.error_class);
  const reason = detail ? `${code}: ${detail}` : code;
  const message = refusal.status === 'rejected'
    ? `The assistant cannot run this turn: the model request was refused before it reached the provider (${reason}). ` +
      'Ask the platform operator to check the budget and model readiness for this persona.'
    : `The assistant could not complete this turn: the model request was sent but its outcome is unknown (${reason}). ` +
      'Please retry in a moment.';
  return message.length <= TURN_ERROR_MESSAGE_MAX_LENGTH ? message : message.slice(0, TURN_ERROR_MESSAGE_MAX_LENGTH);
}

/**
 * Record the failed result for a turn that `error` ended. A rejected or unknown model
 * receipt names its reason; any other error keeps the plain failed result. A gateway that
 * predates `error_message` rejects the field (#7971); the plain failed result still ends the turn.
 */
export async function publishFailedTurn(client: ChatDataClient, error: unknown): Promise<void> {
  const refusal = modelInvokeRefusal(error);
  if (!refusal) {
    await client.submitTurnResult({ outcome: 'failed' });
    return;
  }
  try {
    await client.submitTurnResult({ outcome: 'failed', error_message: modelInvokeFailureMessage(refusal) });
  } catch (failure) {
    if (!(failure instanceof ChatDataError && failure.code === 'invalid_request')) throw failure;
    await client.submitTurnResult({ outcome: 'failed' });
  }
}
