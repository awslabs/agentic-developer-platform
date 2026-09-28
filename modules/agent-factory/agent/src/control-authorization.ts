export interface ControlOrigin {
  principal: string;
  authorityKind: 'human_session' | 'delegated_grant';
}

/** Private journal proof; never exposed in status responses. */
export interface QueuedAuthorization {
  envelope: string;
  action: string;
  command_id: string;
  body_base64: string;
  /** Copied only from a verified envelope, never from the request body. */
  principal?: string;
  authorityKind?: ControlOrigin['authorityKind'];
}

export const MAX_REVALIDATION_MS = 1000;

/**
 * What the live authority re-check answers — Issue #3963.
 *
 * `true`/`false` remains the whole answer for every verb except `abort`, which is
 * why the boolean form is still accepted: a pause either may be delivered or may
 * not, and nothing downstream needs evidence of that decision afterwards.
 *
 * An abort is different, and the difference is the subject of review finding 1. Its
 * decision has to be re-presented later, to a *different* process: the pod exits,
 * and the supervisor that writes the terminal status and deletes the queue message
 * has no access to this re-check's result. Passing a boolean forward means the
 * supervisor is told "an abort was accepted" by the very run that would benefit
 * from saying so — a self-assertion, which a fabricated `delivery: "accepted"`
 * field beside a genuine (but merely *issued*) envelope satisfied exactly as well
 * as a real abort did.
 *
 * `abortReceipt` is the gateway's own signed statement that it accepted this abort,
 * minted only after durable intent was recorded. Carrying it through is what lets
 * the supervisor check the decision instead of taking the pod's word for it.
 */
export type RevalidationOutcome =
  | boolean
  | {
      allowed: boolean;
      /** The gateway-signed proof that this abort was accepted; `abort` only. */
      abortReceipt?: string | null;
    };
