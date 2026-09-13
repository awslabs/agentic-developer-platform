/** Private journal proof; never exposed in status responses. */
export interface QueuedAuthorization {
  envelope: string;
  action: string;
  command_id: string;
  body_base64: string;
}

export const MAX_REVALIDATION_MS = 1000;
