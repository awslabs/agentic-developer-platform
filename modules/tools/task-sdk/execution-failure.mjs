/** Public failure text is static; host/provider prose is never forwarded. */
import { ModelRefusal } from './protocol.mjs';

export function executionFailure(error, hostFailure, persona) {
  const label = persona === 'coding' ? 'Coding' : 'Cyber';
  const cause = hostFailure || error;
  if (cause?.message === 'model_outcome_unknown') {
    return { code: 'model_outcome_unknown', message: `${label} SDK model operation outcome is unknown.` };
  }
  if (cause instanceof ModelRefusal) {
    const messages = {
      budget_exceeded: `${label} SDK model request was refused before provider dispatch because the authorized budget was exceeded.`,
      model_access_denied: `${label} SDK model request was refused before provider dispatch by model access or authorization checks.`,
    };
    if (Object.hasOwn(messages, cause.code)) return { code: 'process_failed', message: messages[cause.code] };
  }
  return { code: 'process_failed', message: error?.message === 'SDK model-turn limit reached before a grounded report was accepted'
    ? error.message : `${label} SDK execution did not complete with confirmed evidence.` };
}
