/**
 * Server-issued chat session identifiers (#5615).
 *
 * A conversation's identifier used to be invented by this browser as
 * `sess-${Date.now()}-${Math.random()...}` and then announced to the server,
 * which adopted whatever it was told. Two things followed from that:
 *
 *   1. The identifier was mostly a clock reading, so it was predictable. An
 *      attacker could create the identifier a victim's browser was about to
 *      choose; the victim's own new conversation was then refused as somebody
 *      else's, locking them out of it.
 *   2. Naming an identifier the server had never seen WAS the creation request,
 *      which is what made that squatting possible at all.
 *
 * Now the server mints the identifier from its own cryptographic random source,
 * stamps the signed-in owner on it at creation, and hands it back. This module
 * is that request. The browser never chooses an identifier, and the identifier
 * it is given is only ever repeated back verbatim.
 *
 * Note what this is NOT: it is not the access control. The ingest Lambda checks
 * the recorded owner on every read, write, reply and upload, so knowing an
 * identifier has never been enough to reach a conversation. Unguessable
 * identifiers supplement that check; they do not replace it.
 */

import { deploymentSetting } from '@/config/runtime';
import { getIdToken, isTokenExpired, refreshToken } from '@/services/auth';

/** How long to wait for the server's identifier before giving up. */
export const CREATE_SESSION_TIMEOUT_MS = 15_000;

export class ChatSessionError extends Error {}

/** Shape of the create-session reply, correlated by `request_id`. */
interface CreateSessionReply {
  request_id?: string;
  session_id?: string;
  error?: string;
}

function newRequestId(): string {
  // Correlation only — it identifies this pending request inside this tab, and
  // is never used as, or mixed into, the session identifier the server issues.
  return `cs-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

async function validIdToken(): Promise<string | null> {
  try {
    const token = getIdToken();
    if (!token || isTokenExpired()) {
      const refreshed = await refreshToken();
      return refreshed?.token ?? null;
    }
    return token;
  } catch {
    return null;
  }
}

/**
 * Ask the server to start a conversation and return the identifier it issues.
 *
 * Rejects rather than falling back to a locally invented identifier. That is
 * deliberate: a fallback would quietly restore the behaviour this change exists
 * to remove, and would do so exactly when the server is unreachable and least
 * able to refuse it. A caller that cannot get an identifier must surface the
 * failure and let the user retry.
 *
 * Runs on its own short-lived socket, because the page's chat socket only
 * exists once a conversation does — the very thing being created here. The
 * identifier is bound to the signed-in user, not to this socket, so the
 * conversation works normally on the page's own connection afterwards (the
 * ingress rebinds the live connection on each message).
 *
 * A retry after a timeout is safe. The server never reuses an identifier, so a
 * reply that was lost in flight leaves an empty, owned, unreferenced row that
 * the sessions table's TTL reaps. The retry gets a NEW identifier; it never
 * joins or rebinds the earlier one.
 */
export function requestServerSessionId(
  timeoutMs: number = CREATE_SESSION_TIMEOUT_MS,
): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const wsBaseUrl = deploymentSetting('VITE_AGENT_WS_URL')?.trim();
    if (!wsBaseUrl) {
      reject(new ChatSessionError('Agent chat is not configured for this deployment.'));
      return;
    }

    let settled = false;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const finish = (err: Error | null, sessionId?: string) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try {
        ws?.close();
      } catch {
        // Already closing — nothing to recover.
      }
      if (err) reject(err);
      else resolve(sessionId as string);
    };

    void validIdToken().then((token) => {
      if (settled) return;
      if (!token) {
        finish(new ChatSessionError('Your session has expired. Please sign in again.'));
        return;
      }

      timer = setTimeout(
        () => finish(new ChatSessionError('Timed out starting a conversation.')),
        timeoutMs,
      );

      const requestId = newRequestId();
      ws = new WebSocket(`${wsBaseUrl}?token=${encodeURIComponent(token)}`);

      ws.onopen = () => {
        ws?.send(JSON.stringify({ action: 'create-session', request_id: requestId }));
      };

      ws.onmessage = (event: MessageEvent) => {
        let reply: CreateSessionReply;
        try {
          reply = JSON.parse(event.data as string) as CreateSessionReply;
        } catch {
          return; // Not a frame we can read — keep waiting for ours.
        }
        // Only accept the reply to THIS request. An uncorrelated frame on a
        // shared socket must not be mistaken for an issued identifier.
        if (reply.request_id !== requestId) return;
        if (typeof reply.session_id === 'string' && reply.session_id) {
          finish(null, reply.session_id);
          return;
        }
        finish(new ChatSessionError(reply.error || 'Could not start a conversation.'));
      };

      ws.onerror = () => {
        // onerror is always followed by onclose; report there so a single
        // failure does not produce two rejections.
      };

      ws.onclose = () => {
        finish(new ChatSessionError('Connection closed before a conversation started.'));
      };
    });
  });
}
