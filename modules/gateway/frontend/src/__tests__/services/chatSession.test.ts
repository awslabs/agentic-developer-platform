/**
 * Server-issued chat session identifiers — the browser's half (#5615).
 *
 * The old code invented a conversation's identifier locally
 * (`sess-${Date.now()}-${Math.random()...}`) and told the server about it. That
 * made the identifier predictable, and because naming an unknown identifier WAS
 * the creation request, an attacker could create the one a victim's browser was
 * about to choose and lock them out of their own new conversation.
 *
 * `requestServerSessionId` replaces that with a request: the server mints the
 * identifier and the browser only ever repeats it back. These tests pin the
 * properties that make the handoff safe rather than merely working —
 * correlation, no local fallback, and no reuse across retries.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  requestServerSessionId,
  ChatSessionError,
  CREATE_SESSION_TIMEOUT_MS,
} from '@/services/chatSession';

// ---------------------------------------------------------------------------
// Mock WebSocket (same pattern as the useAgUiEvents / useAgentChat tests)
// ---------------------------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  url: string;
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
    this.readyState = 3;
  }

  simulateOpen() {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  /** Deliver a raw frame exactly as the server would. */
  simulateRaw(data: string) {
    this.onmessage?.(new MessageEvent('message', { data }));
  }

  simulateMessage(payload: unknown) {
    this.simulateRaw(JSON.stringify(payload));
  }

  simulateClose(code = 1006) {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code }));
  }

  simulateError() {
    this.onerror?.(new Event('error'));
  }
}

vi.mock('@/services/auth', () => ({
  getIdToken: vi.fn(() => 'mock-id-token'),
  isTokenExpired: vi.fn(() => false),
  refreshToken: vi.fn(() => Promise.resolve({ token: 'refreshed-token', expiresAt: '' })),
}));

import { getIdToken, isTokenExpired, refreshToken } from '@/services/auth';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function lastWs(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

/** Wait for the socket to be constructed (the call awaits a token first). */
async function waitForSocket(): Promise<MockWebSocket> {
  await vi.advanceTimersByTimeAsync(10);
  return lastWs();
}

function sentFrame(ws: MockWebSocket, index = 0): Record<string, unknown> {
  return JSON.parse(ws.sent[index]) as Record<string, unknown>;
}

/** A realistic server-issued id: `sess-` + 16 random bytes as hex. */
const ISSUED_ID = 'sess-4f2c8a1e9b7d3056fa1c2e4d6b8a0f93';

describe('requestServerSessionId', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.stubEnv('VITE_AGENT_WS_URL', 'wss://chat.example.test/v1');
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getIdToken).mockReturnValue('mock-id-token');
    vi.mocked(isTokenExpired).mockReturnValue(false);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  // ----- Fresh creation -----

  it('asks the server to create a session and returns the id it issues', async () => {
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();

    const frame = sentFrame(ws);
    expect(frame.action).toBe('create-session');

    ws.simulateMessage({ request_id: frame.request_id, session_id: ISSUED_ID });

    await expect(pending).resolves.toBe(ISSUED_ID);
  });

  it('proposes no identifier of its own', async () => {
    /*
     * The core of the fix. If this frame carried a `session_id`, the browser
     * would still be choosing — and a server that ever honoured the hint would
     * be back to the original defect.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();

    const frame = sentFrame(ws);
    expect(frame).not.toHaveProperty('session_id');
    expect(Object.keys(frame).sort()).toEqual(['action', 'request_id']);

    ws.simulateMessage({ request_id: frame.request_id, session_id: ISSUED_ID });
    await pending;
  });

  it('returns the issued id verbatim, without reshaping it', async () => {
    /*
     * The id is a DynamoDB key AND an S3 path segment on the server side. Any
     * client-side normalisation (trim, lowercase, re-prefix) would silently
     * address a different row than the one that was created for this user.
     */
    const odd = 'sess-00000000000000000000000000000001';
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();
    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: odd });

    await expect(pending).resolves.toBe(odd);
  });

  it('sends the caller credential on the connection, not in the frame', async () => {
    const pending = requestServerSessionId();
    const ws = await waitForSocket();

    expect(ws.url).toContain(`token=${encodeURIComponent('mock-id-token')}`);
    ws.simulateOpen();
    expect(sentFrame(ws)).not.toHaveProperty('token');

    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: ISSUED_ID });
    await pending;
  });

  it('refreshes an expired token rather than connecting with it', async () => {
    /*
     * The identifier is bound to the identity the authorizer verifies at
     * $connect. Connecting with a stale token means no verified owner, so the
     * server has nothing to bind the new conversation to.
     */
    vi.mocked(isTokenExpired).mockReturnValue(true);

    const pending = requestServerSessionId();
    const ws = await waitForSocket();

    expect(refreshToken).toHaveBeenCalled();
    expect(ws.url).toContain(`token=${encodeURIComponent('refreshed-token')}`);

    ws.simulateOpen();
    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: ISSUED_ID });
    await pending;
  });

  // ----- Correlation -----

  it('only accepts the reply to its own request', async () => {
    /*
     * API Gateway discards a WebSocket integration's return body, so the id
     * arrives as a pushed frame. Without checking `request_id`, an unrelated
     * frame carrying a `session_id` — another tab's reply, a late frame from a
     * previous request — could be mistaken for this request's answer and the
     * user would be dropped into a conversation they did not open.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();
    const requestId = sentFrame(ws).request_id;

    ws.simulateMessage({ request_id: 'some-other-request', session_id: 'sess-not-mine' });
    ws.simulateMessage({ session_id: 'sess-uncorrelated' });
    ws.simulateMessage({ request_id: requestId, session_id: ISSUED_ID });

    await expect(pending).resolves.toBe(ISSUED_ID);
  });

  it('ignores unreadable frames and keeps waiting for its own', async () => {
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();

    ws.simulateRaw('not json at all');
    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: ISSUED_ID });

    await expect(pending).resolves.toBe(ISSUED_ID);
  });

  it('gives each request a distinct correlation id', async () => {
    const first = requestServerSessionId();
    const wsA = await waitForSocket();
    wsA.simulateOpen();
    const second = requestServerSessionId();
    const wsB = await waitForSocket();
    wsB.simulateOpen();

    const idA = sentFrame(wsA).request_id;
    const idB = sentFrame(wsB).request_id;
    expect(idA).not.toBe(idB);

    wsA.simulateMessage({ request_id: idA, session_id: `${ISSUED_ID}a` });
    wsB.simulateMessage({ request_id: idB, session_id: `${ISSUED_ID}b` });
    await expect(first).resolves.toBe(`${ISSUED_ID}a`);
    await expect(second).resolves.toBe(`${ISSUED_ID}b`);
  });

  // ----- Failure: never fall back to a local identifier -----

  it('rejects on timeout instead of inventing an identifier', async () => {
    /*
     * The regression that would undo this whole change. A fallback would fire
     * exactly when the server is unreachable — i.e. when it is least able to
     * refuse a client-chosen id — and would restore the predictable
     * clock-derived identifier the issue is about.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();

    const assertion = expect(pending).rejects.toThrow(ChatSessionError);
    await vi.advanceTimersByTimeAsync(CREATE_SESSION_TIMEOUT_MS + 100);
    await assertion;
  });

  it('closes the socket it opened when it gives up', async () => {
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();

    const assertion = expect(pending).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(CREATE_SESSION_TIMEOUT_MS + 100);
    await assertion;

    expect(ws.closed).toBe(true);
  });

  it('rejects when the connection closes before an id arrives', async () => {
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();
    ws.simulateClose(1006);

    await expect(pending).rejects.toThrow(ChatSessionError);
  });

  it('reports a single failure once when the socket errors then closes', async () => {
    /*
     * `onerror` is always followed by `onclose`. Rejecting in both would settle
     * the promise twice and, in a caller that retries on rejection, could start
     * two conversations for one click.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();

    const rejection = expect(pending).rejects.toThrow(ChatSessionError);
    ws.simulateError();
    ws.simulateClose(1006);
    await rejection;
    // A late reply after settling must not resolve it retroactively.
    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: ISSUED_ID });
  });

  it('surfaces the server’s refusal rather than retrying blindly', async () => {
    /*
     * The server refuses to issue an id when it cannot express an owner for it
     * (no tenant) — `handle_create_session` returns 503. Retrying would fail
     * identically, so the user needs to see the reason.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();
    ws.simulateMessage({
      request_id: sentFrame(ws).request_id,
      error: 'could not start a conversation',
    });

    await expect(pending).rejects.toThrow('could not start a conversation');
  });

  it('treats a reply with no id as a failure, not as an empty id', async () => {
    /*
     * An empty-string id would be accepted by `if (session_id)` only if the
     * check were loose; it would then be sent as the conversation key and every
     * message would be refused.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();
    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: '' });

    await expect(pending).rejects.toThrow(ChatSessionError);
  });

  it('does not open a socket at all when chat is unconfigured', async () => {
    /*
     * No endpoint means no credential should leave the browser, and certainly
     * no locally invented identifier should be handed back as if it worked.
     */
    vi.stubEnv('VITE_AGENT_WS_URL', '');

    await expect(requestServerSessionId()).rejects.toThrow(ChatSessionError);
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('does not open a socket when there is no usable credential', async () => {
    vi.mocked(getIdToken).mockReturnValue(null as unknown as string);
    vi.mocked(isTokenExpired).mockReturnValue(true);
    vi.mocked(refreshToken).mockResolvedValue(null as never);

    const pending = requestServerSessionId();
    const assertion = expect(pending).rejects.toThrow(ChatSessionError);
    await vi.advanceTimersByTimeAsync(10);
    await assertion;

    expect(MockWebSocket.instances).toHaveLength(0);
  });

  // ----- Lost reply and retry -----

  it('a retry after a lost reply gets a new id and never reuses the first', async () => {
    /*
     * The lost-response case. The browser cannot tell "the server never created
     * one" from "the reply was dropped", so a retry is simply another create.
     * What must hold is that it does not resurrect or guess the earlier id: the
     * server never reuses one, and the orphaned row is owned and TTL-reaped.
     */
    const first = requestServerSessionId();
    const wsA = await waitForSocket();
    wsA.simulateOpen();
    // The server issued `${ISSUED_ID}` here, but the frame never arrived.
    const firstAssertion = expect(first).rejects.toThrow(ChatSessionError);
    await vi.advanceTimersByTimeAsync(CREATE_SESSION_TIMEOUT_MS + 100);
    await firstAssertion;

    const retryId = 'sess-11112222333344445555666677778888';
    const second = requestServerSessionId();
    const wsB = await waitForSocket();
    wsB.simulateOpen();

    // The retry asks for a session in exactly the same way — no reference to
    // the attempt that timed out.
    const retryFrame = sentFrame(wsB);
    expect(retryFrame).not.toHaveProperty('session_id');
    expect(JSON.stringify(retryFrame)).not.toContain(ISSUED_ID);

    wsB.simulateMessage({ request_id: retryFrame.request_id, session_id: retryId });
    await expect(second).resolves.toBe(retryId);
  });

  it('a late reply to the abandoned attempt cannot resolve the retry', async () => {
    /*
     * Two sockets, two correlation ids. If the first socket's delayed frame
     * could satisfy the second request, the user would end up on the orphaned
     * identifier — the one the server considers abandoned.
     */
    const first = requestServerSessionId();
    const wsA = await waitForSocket();
    wsA.simulateOpen();
    const staleRequestId = sentFrame(wsA).request_id;
    const firstAssertion = expect(first).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(CREATE_SESSION_TIMEOUT_MS + 100);
    await firstAssertion;

    const second = requestServerSessionId();
    const wsB = await waitForSocket();
    wsB.simulateOpen();

    // The abandoned attempt's reply arrives on the NEW socket, late.
    wsB.simulateMessage({ request_id: staleRequestId, session_id: 'sess-orphaned-row' });
    wsB.simulateMessage({ request_id: sentFrame(wsB).request_id, session_id: ISSUED_ID });

    await expect(second).resolves.toBe(ISSUED_ID);
  });

  it('uses a short-lived socket per request and leaves none open', async () => {
    /*
     * The page's chat socket only exists once a conversation does — the thing
     * being created here — so creation runs on its own connection. Leaking them
     * would accumulate live connections (and connection-claims rows) per click.
     */
    const pending = requestServerSessionId();
    const ws = await waitForSocket();
    ws.simulateOpen();
    ws.simulateMessage({ request_id: sentFrame(ws).request_id, session_id: ISSUED_ID });
    await pending;

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(ws.closed).toBe(true);
  });
});
