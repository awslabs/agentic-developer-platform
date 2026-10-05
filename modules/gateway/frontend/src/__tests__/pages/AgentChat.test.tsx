/**
 * AgentChat page — the server-issued session handoff (#5615).
 *
 * The page used to open a conversation by inventing its identifier
 * (`sess-${Date.now()}-${Math.random()...}`) and adding it to the sidebar
 * immediately. Now it asks the server for one. That turns "start a
 * conversation" from a synchronous local act into a round-trip that can be slow
 * or fail, and these tests cover the consequences of that: what the user sees
 * while it is in flight, what is left behind when it fails, and — the point of
 * the change — that no identifier is ever invented locally as a fallback.
 *
 * The conversation list is persisted in localStorage, so a half-created entry
 * would not merely look wrong for a moment; it would stay on screen across
 * reloads as a conversation the server has never heard of.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import AgentChat from '@/pages/AgentChat';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/services/chatSession', async () => {
  const actual = await vi.importActual<typeof import('@/services/chatSession')>(
    '@/services/chatSession',
  );
  return { ...actual, requestServerSessionId: vi.fn() };
});

/**
 * The chat socket is stubbed out. These tests are about the CREATION handoff,
 * and `useAgUiEvents` has its own suite for the streaming/refusal behaviour.
 */
const mockSendMessage = vi.fn();
let mockConnectionStatus = 'connected';
let mockSessionExpired = false;

vi.mock('@/hooks/useAgUiEvents', () => ({
  useAgUiEvents: vi.fn(() => ({
    connectionStatus: mockConnectionStatus,
    isAwaitingReply: false,
    reconnectAttempt: 0,
    sessionExpired: mockSessionExpired,
    sessionMeta: null,
    sendMessage: mockSendMessage,
    activeToolCalls: [],
    wsRef: { current: null },
  })),
}));

import { requestServerSessionId, ChatSessionError } from '@/services/chatSession';

const mockRequestId = vi.mocked(requestServerSessionId);

const STORAGE_KEY = 'adp_chat_conversations';
const ISSUED_ID = 'sess-4f2c8a1e9b7d3056fa1c2e4d6b8a0f93';

function renderPage() {
  return render(
    <MemoryRouter>
      <AgentChat />
    </MemoryRouter>,
  );
}

function storedConversations(): Array<{ id: string; title: string }> {
  return JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '[]');
}

/** A deferred promise, so a creation can be held mid-flight. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('AgentChat — server-issued session ids', () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockSendMessage.mockClear();
    mockRequestId.mockReset();
    mockConnectionStatus = 'connected';
    mockSessionExpired = false;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  // ----- Fresh creation -----

  it('starts a conversation under the identifier the server issues', async () => {
    const user = userEvent.setup();
    mockRequestId.mockResolvedValue(ISSUED_ID);
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));

    await waitFor(() => expect(storedConversations()).toHaveLength(1));
    expect(storedConversations()[0].id).toBe(ISSUED_ID);
  });

  it('never derives an identifier of its own', async () => {
    /*
     * The defect, stated as a test. `Date.now()`-derived ids are recognisable:
     * 13 digits of millisecond clock. If one ever reappears in the stored
     * conversation list, the page has gone back to naming its own sessions.
     */
    const user = userEvent.setup();
    mockRequestId.mockResolvedValue(ISSUED_ID);
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));
    await waitFor(() => expect(storedConversations()).toHaveLength(1));

    expect(storedConversations()[0].id).not.toMatch(/^sess-\d{13}-/);
    expect(mockRequestId).toHaveBeenCalled();
  });

  // ----- In-flight -----

  it('adds nothing to the sidebar until the server has acknowledged an id', async () => {
    /*
     * Ordering rule. An entry added first and patched later would be a
     * conversation on screen that the server has never heard of; a lost or
     * failed reply would strand it there permanently, clickable and unusable.
     */
    const user = userEvent.setup();
    const gate = deferred<string>();
    mockRequestId.mockReturnValue(gate.promise);
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));

    expect(storedConversations()).toHaveLength(0);

    await act(async () => {
      gate.resolve(ISSUED_ID);
      await gate.promise;
    });

    await waitFor(() => expect(storedConversations()).toHaveLength(1));
  });

  it('disables the composer while a conversation is being created', async () => {
    const user = userEvent.setup();
    const gate = deferred<string>();
    mockRequestId.mockReturnValue(gate.promise);
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));

    expect(screen.getByTestId('chat-input')).toBeDisabled();

    await act(async () => {
      gate.resolve(ISSUED_ID);
      await gate.promise;
    });

    await waitFor(() => expect(screen.getByTestId('chat-input')).not.toBeDisabled());
  });

  it('creates only one conversation when the button is clicked repeatedly', async () => {
    /*
     * Each accepted click would mint a real server-side row. Without the
     * in-flight guard an impatient double-click leaves owned orphan rows behind
     * — harmless individually, but it is debris the user cannot see or clean up.
     */
    const user = userEvent.setup();
    const gate = deferred<string>();
    mockRequestId.mockReturnValue(gate.promise);
    renderPage();

    const button = screen.getByTestId('new-conversation-button');
    await user.click(button);
    await user.click(button);
    await user.click(button);

    expect(mockRequestId).toHaveBeenCalledTimes(1);

    await act(async () => {
      gate.resolve(ISSUED_ID);
      await gate.promise;
    });
    await waitFor(() => expect(storedConversations()).toHaveLength(1));
  });

  // ----- Failure and retry -----

  it('reports a failure and leaves no conversation behind', async () => {
    const user = userEvent.setup();
    mockRequestId.mockRejectedValue(new ChatSessionError('Timed out starting a conversation.'));
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));

    await waitFor(() =>
      expect(screen.getByTestId('start-session-error')).toHaveTextContent(
        'Timed out starting a conversation.',
      ),
    );
    expect(storedConversations()).toHaveLength(0);
  });

  it('a retry after a lost reply uses the new identifier, not the old one', async () => {
    /*
     * The lost-response path end to end. The first attempt may well have
     * created a row server-side whose reply never arrived; the retry must take
     * the NEW id. The orphan stays owned by this user and is reaped by the
     * sessions table's TTL, so it is not adoptable in the meantime.
     */
    const user = userEvent.setup();
    const retryId = 'sess-11112222333344445555666677778888';
    mockRequestId
      .mockRejectedValueOnce(new ChatSessionError('Timed out starting a conversation.'))
      .mockResolvedValueOnce(retryId);
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));
    await waitFor(() => screen.getByTestId('start-session-retry'));

    await user.click(screen.getByTestId('start-session-retry'));

    await waitFor(() => expect(storedConversations()).toHaveLength(1));
    expect(storedConversations()[0].id).toBe(retryId);
    expect(mockRequestId).toHaveBeenCalledTimes(2);
  });

  it('clears the failure notice once a conversation starts', async () => {
    const user = userEvent.setup();
    mockRequestId
      .mockRejectedValueOnce(new ChatSessionError('Timed out starting a conversation.'))
      .mockResolvedValueOnce(ISSUED_ID);
    renderPage();

    await user.click(screen.getByTestId('new-conversation-button'));
    await waitFor(() => screen.getByTestId('start-session-retry'));
    await user.click(screen.getByTestId('start-session-retry'));

    await waitFor(() => expect(screen.queryByTestId('start-session-error')).toBeNull());
  });

  // ----- First message -----

  it('keeps the user’s typing when creation fails, so nothing is lost', async () => {
    /*
     * Typing then pressing Enter with no conversation open triggers creation.
     * If the text were cleared optimistically, a failed creation would discard
     * whatever the user had written.
     */
    const user = userEvent.setup();
    mockRequestId.mockRejectedValue(new ChatSessionError('Timed out starting a conversation.'));
    renderPage();

    const input = screen.getByTestId('chat-input');
    await user.type(input, 'Review my PR{Enter}');

    await waitFor(() => screen.getByTestId('start-session-error'));
    expect(input).toHaveValue('Review my PR');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('does not send the first message until an identifier exists', async () => {
    /*
     * The send is gated on the server-issued id, so the first message of a
     * conversation can only ever be addressed to an id the server acknowledged.
     */
    const user = userEvent.setup();
    const gate = deferred<string>();
    mockRequestId.mockReturnValue(gate.promise);
    renderPage();

    await user.type(screen.getByTestId('chat-input'), 'Hello there{Enter}');

    expect(mockSendMessage).not.toHaveBeenCalled();
    expect(storedConversations()).toHaveLength(0);

    await act(async () => {
      gate.resolve(ISSUED_ID);
      await gate.promise;
    });
    await waitFor(() => expect(storedConversations()[0].id).toBe(ISSUED_ID));
  });

  // ----- Existing conversations -----

  it('reuses an existing conversation instead of asking for a new id', async () => {
    /*
     * Reconnect/compatibility. A conversation the user already owns — including
     * one created before this change, under an old clock-derived id — must keep
     * working, and typing into it must not mint a new session.
     */
    const legacyId = 'sess-1758441600000-a1b2c3d';
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        { id: legacyId, title: 'Yesterday', createdAt: 1, updatedAt: 1, messages: [] },
      ]),
    );
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByTestId('chat-input'), 'Still here?{Enter}');

    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledWith('Still here?', undefined));
    expect(mockRequestId).not.toHaveBeenCalled();
    expect(storedConversations()).toHaveLength(1);
  });

  // ----- Refused identifier -----

  it('offers a new conversation when the server refuses this one', async () => {
    /*
     * The honest consequence of no longer creating unknown ids: an id whose row
     * has expired cannot be revived, so the only recovery is a new
     * conversation. Retrying the same id would be a guaranteed refusal.
     */
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        { id: ISSUED_ID, title: 'Expired', createdAt: 1, updatedAt: 1, messages: [] },
      ]),
    );
    mockSessionExpired = true;
    const freshId = 'sess-99998888777766665555444433332222';
    mockRequestId.mockResolvedValue(freshId);
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByTestId('session-expired-notice')).toBeInTheDocument();
    expect(screen.getByTestId('chat-input')).toBeDisabled();

    await user.click(screen.getByTestId('session-expired-new'));

    await waitFor(() => expect(storedConversations()[0].id).toBe(freshId));
  });

  it('does not reveal whether a refused conversation ever existed', async () => {
    /*
     * The server answers identically for an expired id and for another
     * tenant's (#5742). The page must not editorialise that into "expired" or
     * "not yours", which would make the UI an enumeration oracle the API
     * deliberately is not.
     */
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        { id: ISSUED_ID, title: 'Refused', createdAt: 1, updatedAt: 1, messages: [] },
      ]),
    );
    mockSessionExpired = true;
    renderPage();

    const notice = screen.getByTestId('session-expired-notice').textContent?.toLowerCase() ?? '';
    expect(notice).not.toMatch(/expired|another user|other user|belongs to|forbidden|deleted/);
  });
});
