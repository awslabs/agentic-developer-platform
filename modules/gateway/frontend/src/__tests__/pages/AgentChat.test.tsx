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
import { render, screen, waitFor, act, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import AgentChat from '@/pages/AgentChat';
import { useAgUiEvents } from '@/hooks/useAgUiEvents';
import type { ToolCallInfo } from '@/types/ag-ui-events';
import type { ChatTurnPhase } from '@/types/chat';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/services/chatSession', async () => {
  const actual = await vi.importActual<typeof import('@/services/chatSession')>(
    '@/services/chatSession',
  );
  return { ...actual, requestServerSessionId: vi.fn() };
});

vi.mock('@/services/products', () => ({ assignChatProject: vi.fn() }));

vi.mock('@/services/chatMode', () => ({ readChatMode: vi.fn(), selectChatMode: vi.fn(), endChatSession: vi.fn() }));

const mockSetRuntimeChoice = vi.fn();
let mockRuntimeChoice = 'adp-assistant';
let mockCrewAllowed = false;
vi.mock('@/hooks/useCrewRuntime', async () => {
  const actual = await vi.importActual<typeof import('@/hooks/useCrewRuntime')>('@/hooks/useCrewRuntime');
  return { ...actual, useCrewRuntime: () => ({
    choice: mockRuntimeChoice, setChoice: mockSetRuntimeChoice, allowed: mockCrewAllowed, hostAllowed: false, loading: false,
    options: actual.RUNTIME_OPTIONS.filter(option => !option.requires || (option.requires === 'eks-pod' && mockCrewAllowed)),
  }) };
});

let crewEnabled = false;
vi.mock('@/components/chat/CrewRuntimeAction', () => ({ default: () => <button>Open my Crew</button> }));

vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => ({ assistant_ui: crewEnabled, crew_dashboard: crewEnabled,
    crew_dashboard_origin: crewEnabled ? 'https://crew.example.test' : undefined }),
}));

/**
 * The chat socket is stubbed out. These tests are about the CREATION handoff,
 * and `useAgUiEvents` has its own suite for the streaming/refusal behaviour.
 */
const mockSendMessage = vi.fn();
const mockRetryConnection = vi.fn();
let mockConnectionStatus = 'connected';
let mockReconnectAttempt = 0;
let mockSessionExpired = false;
let mockReplayWarning: string | null = null;
let mockAwaitingReply = false;
let mockCancelState = 'idle';
let mockTurnPhase: ChatTurnPhase | null = null;
let mockActiveToolCalls: ToolCallInfo[] = [];
const mockCancelTurn = vi.fn();

vi.mock('@/hooks/useAgUiEvents', () => ({
  useAgUiEvents: vi.fn(() => ({
    connectionStatus: mockConnectionStatus,
    isAwaitingReply: mockAwaitingReply,
    turnPhase: mockTurnPhase,
    canCancel: mockAwaitingReply && mockCancelState !== 'requested',
    cancelState: mockCancelState,
    cancelTurn: mockCancelTurn,
    reconnectAttempt: mockReconnectAttempt,
    retryConnection: mockRetryConnection,
    sessionExpired: mockSessionExpired,
    replayWarning: mockReplayWarning,
    sessionMeta: null,
    sendMessage: mockSendMessage,
    activeToolCalls: mockActiveToolCalls,
    wsRef: { current: null },
  })),
}));

import { assignChatProject } from '@/services/products';
import { requestServerSessionId, ChatSessionError } from '@/services/chatSession';
import { readChatMode, selectChatMode, endChatSession } from '@/services/chatMode';

const mockRequestId = vi.mocked(requestServerSessionId);
const mockReadChatMode = vi.mocked(readChatMode);
const mockSelectChatMode = vi.mocked(selectChatMode);
const mockEndChatSession = vi.mocked(endChatSession);

const STORAGE_KEY = 'adp_chat_conversations';
const ISSUED_ID = 'sess-4f2c8a1e9b7d3056fa1c2e4d6b8a0f93';
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

it('keeps Open my Crew behind the assistant and Crew feature gates', () => {
  const page = renderPage();
  expect(screen.queryByRole('button', { name: 'Open my Crew' })).not.toBeInTheDocument();
  page.unmount();
  crewEnabled = true;
  renderPage();
  expect(screen.getByRole('button', { name: 'Open my Crew' })).toBeInTheDocument();
  crewEnabled = false;
});

function renderPage(activityId?: string, installationId?: string) {
  return render(
    <MemoryRouter>
      <AgentChat activityId={activityId} installationId={installationId} />
    </MemoryRouter>,
  );
}

function storedConversations(): Array<{ id: string; title: string }> {
  return JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '[]');
}

function simulateDisabledComposerFocusLoss() {
  const previousTabIndex = document.body.getAttribute('tabindex');
  document.body.tabIndex = -1;
  act(() => document.body.focus());
  if (previousTabIndex === null) document.body.removeAttribute('tabindex');
  else document.body.setAttribute('tabindex', previousTabIndex);
  expect(document.body).toHaveFocus();
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
    HTMLElement.prototype.scrollIntoView = vi.fn();
    window.localStorage.clear();
    mockSendMessage.mockReset().mockReturnValue(true);
    mockRequestId.mockReset();
    mockReadChatMode.mockReset().mockResolvedValue({ mode: 'ephemeral', health: 'idle', sequence: 0 });
    mockSelectChatMode.mockReset().mockResolvedValue({ mode: 'persistent', health: 'idle', sequence: 0, executor: 'claude-code' });
    mockEndChatSession.mockReset().mockResolvedValue({ mode: 'persistent', health: 'ending', sequence: 1 });
    mockSetRuntimeChoice.mockReset();
    mockRuntimeChoice = 'adp-assistant';
    mockCrewAllowed = false;
    mockConnectionStatus = 'connected';
    mockReconnectAttempt = 0;
    mockSessionExpired = false;
    mockReplayWarning = null;
    mockAwaitingReply = false;
    mockTurnPhase = null;
    mockRetryConnection.mockReset();
    mockActiveToolCalls = [];
    mockCancelState = 'idle';
    mockCancelTurn.mockReset();
  });

  afterEach(() => {
    HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    vi.clearAllMocks();
  });

  it('binds a Product session before making it available for messages', async () => {
    mockRequestId.mockResolvedValue(ISSUED_ID);
    const binding = deferred<{ product_id: string; project_id: string }>();
    vi.mocked(assignChatProject).mockReturnValue(binding.promise);
    render(<MemoryRouter initialEntries={['/chat?product=product-1&project=project-2']}><AgentChat /></MemoryRouter>);
    await waitFor(() => expect(assignChatProject).toHaveBeenCalledWith(ISSUED_ID, 'product-1', 'project-2'));
    expect(storedConversations()).toEqual([]);
    await act(async () => binding.resolve({ product_id: 'product-1', project_id: 'project-2' }));
    await waitFor(() => expect(storedConversations()[0]?.id).toBe(ISSUED_ID));
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('selects server-owned mode and restores it on reload without a local mode flag', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const page = renderPage();
    const selector = await screen.findByRole('combobox', { name: 'Session runtime' });
    await waitFor(() => expect(selector).toBeEnabled());
    // The four runtimes of #8115; Kiro Crew options follow the tenant's Crew policy.
    expect(within(selector).getAllByRole('option').map(option => option.textContent)).toEqual([
      'ADP assistant (quick answers)', 'Claude Code in a sandbox (persistent, tools and workspace)']);
    await userEvent.setup().selectOptions(selector, 'claude-code');
    // Choosing Claude Code switches the session to persistent and stores the executor server-side.
    await waitFor(() => expect(mockSelectChatMode).toHaveBeenCalledWith(ISSUED_ID, 'persistent', 'claude-code'));
    expect(mockSetRuntimeChoice).toHaveBeenCalledWith('claude-code');
    await waitFor(() => expect(selector).toHaveValue('claude-code'));
    expect(screen.getByRole('status', { name: 'Persistent session idle' })).toHaveTextContent('idle · Claude Code');
    expect(storedConversations()[0]).not.toHaveProperty('sessionMode');
    page.unmount();
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1, executor: 'claude-code' });
    renderPage();
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Session runtime' })).toHaveValue('claude-code'));
    expect(screen.getByRole('status', { name: 'Persistent session active' })).toHaveTextContent('active · Claude Code');
  });

  it('switches a persistent conversation back to the assistant and offers Kiro Crew only when permitted', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockCrewAllowed = true;
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'idle', sequence: 1, executor: 'claude-code' });
    mockSelectChatMode.mockResolvedValue({ mode: 'ephemeral', health: 'idle', sequence: 1 });
    renderPage();
    const selector = await screen.findByRole('combobox', { name: 'Session runtime' });
    await waitFor(() => expect(selector).toHaveValue('claude-code'));
    expect(within(selector).getAllByRole('option').map(option => option.textContent)).toEqual([
      'ADP assistant (quick answers)', 'Claude Code in a sandbox (persistent, tools and workspace)', 'Kiro Crew on EKS']);
    await userEvent.setup().selectOptions(selector, 'adp-assistant');
    await waitFor(() => expect(mockSelectChatMode).toHaveBeenCalledWith(ISSUED_ID, 'ephemeral', undefined));
    await waitFor(() => expect(selector).toHaveValue('adp-assistant'));
    expect(screen.queryByRole('status', { name: /Persistent session/ })).not.toBeInTheDocument();
    mockSelectChatMode.mockResolvedValue({ mode: 'persistent', health: 'idle', sequence: 1, executor: 'kirocrew' });
    await userEvent.setup().selectOptions(selector, 'kirocrew-eks');
    await waitFor(() => expect(mockSelectChatMode).toHaveBeenCalledWith(ISSUED_ID, 'persistent', 'kirocrew'));
    await waitFor(() => expect(selector).toHaveValue('kirocrew-eks'));
    expect(screen.getByRole('status', { name: 'Persistent session idle' })).toHaveTextContent('Kiro Crew');
  });

  it('names a persistent session that never chose an executor as running the deployment default', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1 });
    renderPage();
    const selector = await screen.findByRole('combobox', { name: 'Session runtime' });
    await waitFor(() => expect(selector).toHaveValue('persistent-default'));
    expect(within(selector).getByRole('option', { name: 'Persistent sandbox (deployment default)' })).toBeDisabled();
    expect(screen.getByRole('status', { name: 'Persistent session active' })).toHaveTextContent(/^active$/);
    // Re-selecting the same assistant runtime on a persistent session is a real change (ephemeral).
    await userEvent.setup().selectOptions(selector, 'adp-assistant');
    await waitFor(() => expect(mockSelectChatMode).toHaveBeenCalledWith(ISSUED_ID, 'ephemeral', undefined));
  });

  it('shows the backend idle limit and cost warning only for an active persistent session', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1, idle_timeout_seconds: 300 });
    renderPage();
    expect(await screen.findByRole('note')).toHaveTextContent('5 minutes');
    expect(screen.getByRole('note')).toHaveTextContent('capacity costs');
    expect(screen.getByRole('status', { name: 'Persistent session active' })).toBeInTheDocument();
    await userEvent.setup().type(screen.getByTestId('chat-input'), 'Please save this draft');
    await userEvent.setup().click(screen.getByRole('button', { name: 'End session' }));
    await waitFor(() => expect(mockEndChatSession).toHaveBeenCalledWith(ISSUED_ID));
    expect(screen.getByTestId('chat-input')).toHaveValue('Please save this draft');
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Persistent session ending' })).toBeInTheDocument();
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('does not invent an idle limit when the backend does not report one', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1 });
    renderPage();
    expect(await screen.findByRole('status', { name: 'Persistent session active' })).toBeInTheDocument();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
  it('ends a persistent session through the authenticated action and shows cleanup progress', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1 });
    renderPage();
    await userEvent.setup().click(await screen.findByRole('button', { name: 'End session' }));
    await waitFor(() => expect(mockEndChatSession).toHaveBeenCalledWith(ISSUED_ID));
    expect(await screen.findByRole('status', { name: 'Persistent session ending' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'End session' })).not.toBeInTheDocument();
  });

  it('treats /exit as an end action without sending it as a model turn', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1 });
    renderPage();
    await waitFor(() => expect(screen.getByTestId('chat-input')).toBeEnabled());
    await userEvent.setup().type(screen.getByTestId('chat-input'), '/exit{Enter}');
    await waitFor(() => expect(mockEndChatSession).toHaveBeenCalledWith(ISSUED_ID));
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('keeps a pending mode switch visible until backend confirms cleanup', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', sequence: 1, executor: 'claude-code' });
    mockSelectChatMode.mockResolvedValue({ mode: 'persistent', health: 'active', pending_mode: 'ephemeral', sequence: 1, executor: 'claude-code' });
    renderPage();
    const selector = await screen.findByRole('combobox', { name: 'Session runtime' });
    await waitFor(() => expect(selector).toBeEnabled());
    await userEvent.setup().selectOptions(selector, 'adp-assistant');
    expect(await screen.findByText(/waiting for sandbox cleanup/)).toBeInTheDocument();
    expect(selector).toHaveValue('claude-code');
    expect(selector).toBeDisabled();
    expect(screen.getByTestId('chat-input')).toBeDisabled();
  });

  it('surfaces a cleanup delay and disables new turns until removal is confirmed', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'cleanup_delayed', sequence: 1, cleanup_elapsed_seconds: 123 });
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('removal not confirmed (123s)');
    expect(screen.getByTestId('chat-input')).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Session runtime' })).toBeDisabled();
  });

  it('shows an explicit replay retention gap above the saved conversation', () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [], replayCursor: `${'a'.repeat(32)}:3` },
    ]));
    mockReplayWarning = 'Some output is unavailable. This transcript may be incomplete.';
    renderPage();
    expect(screen.getByRole('alert')).toHaveTextContent('This transcript may be incomplete.');
  });

  it('sends the first message once after the server issues a session, not again on reconnect', async () => {
    mockConnectionStatus = 'disconnected';
    mockRequestId.mockResolvedValue(ISSUED_ID);
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'First question{Enter}');
    await waitFor(() => expect(storedConversations()[0].id).toBe(ISSUED_ID));
    mockConnectionStatus = 'connecting';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledWith('First question'));
    expect(mockSendMessage).toHaveBeenCalledTimes(1);
    await user.type(screen.getByTestId('chat-input'), 'Next draft');
    mockConnectionStatus = 'reconnecting';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(mockSendMessage).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('chat-input')).toHaveValue('Next draft');
  });
  it('keeps the draft when the socket refuses to send', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    mockSendMessage.mockReturnValueOnce(false);
    renderPage();
    const input = screen.getByTestId('chat-input');
    await user.type(input, 'Keep this until accepted{Enter}');
    expect(input).toHaveValue('Keep this until accepted');
    await user.click(screen.getByTestId('send-button'));
    expect(input).toHaveValue('');
    expect(mockSendMessage).toHaveBeenCalledTimes(2);
  });
  it('preserves a draft across a dropped connection and requires an explicit send after retry', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Keep this unsent');
    mockConnectionStatus = 'disconnected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-recovery')).toHaveTextContent('unsent text is preserved');
    await user.type(screen.getByTestId('chat-input'), '{Enter}');
    expect(mockSendMessage).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Retry connection' }));
    expect(mockRetryConnection).toHaveBeenCalledOnce();
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-input')).toHaveValue('Keep this unsent');
    expect(mockSendMessage).not.toHaveBeenCalled();
    await user.click(screen.getByTestId('send-button'));
    expect(mockSendMessage).toHaveBeenCalledOnce();
    expect(mockSendMessage).toHaveBeenCalledWith('Keep this unsent', undefined);
  });

  it('provides one review action for a replay gap while connected without resending the draft', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Do not resend me');
    mockReplayWarning = 'Some output is unavailable. This transcript may be incomplete.';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.getByTestId('chat-recovery')).toHaveTextContent('incomplete');
    expect(screen.queryByRole('button', { name: 'Retry connection' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Review conversation' }));
    expect(screen.getByRole('log', { name: 'Chat messages' })).toHaveFocus();
    expect(screen.getByTestId('chat-input')).toHaveValue('Do not resend me');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it.each(['replay gap', 'failed turn', 'interrupted turn'] as const)('offers reconnect after exhausted retries with a %s', async (failure) => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Keep this draft');
    if (failure === 'replay gap') mockReplayWarning = 'Some output is unavailable. This transcript may be incomplete.';
    else mockTurnPhase = failure === 'failed turn' ? 'failed' : 'interrupted';
    mockReconnectAttempt = 10;
    mockConnectionStatus = 'disconnected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    const recovery = screen.getByTestId('chat-recovery');
    expect(recovery).toHaveTextContent(failure === 'replay gap' ? 'transcript may be incomplete' : 'reply stopped unexpectedly');
    expect(recovery).toHaveTextContent('Chat is disconnected');
    expect(within(recovery).getAllByRole('button')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Review conversation' })).not.toBeInTheDocument();
    expect(screen.getByTestId('send-button')).toBeDisabled();
    await user.type(screen.getByTestId('chat-input'), '{Enter}');
    expect(mockSendMessage).not.toHaveBeenCalled();
    await user.click(within(recovery).getByRole('button', { name: 'Retry connection' }));
    expect(mockRetryConnection).toHaveBeenCalledOnce();
    expect(mockSendMessage).not.toHaveBeenCalled();
    expect(mockRequestId).not.toHaveBeenCalled();
    expect(screen.getByTestId('chat-input')).toHaveValue('Keep this draft');
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-recovery')).toHaveTextContent(failure === 'replay gap' ? 'transcript may be incomplete' : 'reply stopped unexpectedly');
    expect(screen.getByRole('button', { name: 'Retry connection' })).toBeEnabled();
  });
  it('announces turn changes and restores draft focus after a disconnected retry', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [
        { id: 'reply', role: 'assistant', content: 'Prior answer', status: 'complete', timestamp: 2,
          evidence: { status: 'partial', coverage: [{ source: 'activity.adp', status: 'partial' }] } },
      ] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    const log = screen.getByRole('log', { name: 'Chat messages' });
    expect(log).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByRole('region', { name: 'Answer evidence' })).toHaveTextContent('Partial results');
    await user.type(screen.getByTestId('chat-input'), 'Unsent follow-up');
    mockTurnPhase = 'streaming';
    mockAwaitingReply = true;
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('turn-progress')).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByTestId('turn-progress')).toHaveTextContent('Streaming answer');
    mockAwaitingReply = false;
    mockConnectionStatus = 'disconnected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-recovery')).toHaveAttribute('role', 'alert');
    await user.click(screen.getByRole('button', { name: 'Retry connection' }));
    mockConnectionStatus = 'connecting';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-input')).toHaveFocus();
    expect(screen.getByTestId('chat-input')).toHaveValue('Unsent follow-up');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });
  it('restores focus lost by the disabled composer after automatic transport reconnection', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Unsent question');
    expect(screen.getByTestId('chat-input')).toHaveFocus();
    mockConnectionStatus = 'reconnecting';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-input')).toBeDisabled();
    simulateDisabledComposerFocusLoss();
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-input')).toHaveFocus();
    expect(screen.getByTestId('chat-input')).toHaveValue('Unsent question');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });
  it('does not steal focus when the user moved away before a reconnect', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Saved draft');
    await user.tab();
    expect(screen.getByTestId('send-button')).toHaveFocus();
    mockConnectionStatus = 'reconnecting';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-input')).not.toHaveFocus();
    expect(screen.getByTestId('chat-input')).toHaveValue('Saved draft');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });
  it.each(['sidebar', 'sidebar after blur', 'keyboard', 'outside chat'])('respects focus moved during reconnect to %s', async (destination) => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const user = userEvent.setup();
    const view = () => <MemoryRouter><button type="button">Other page control</button><AgentChat /></MemoryRouter>;
    const page = render(view());
    await user.type(screen.getByTestId('chat-input'), 'Keep my draft');
    mockConnectionStatus = 'reconnecting';
    page.rerender(view());
    expect(screen.getByTestId('chat-input')).toBeDisabled();
    if (destination === 'sidebar after blur') {
      simulateDisabledComposerFocusLoss();
    }
    if (destination === 'keyboard') await user.tab({ shift: true });
    else if (destination === 'outside chat') await user.click(screen.getByRole('button', { name: 'Other page control' }));
    else await user.click(screen.getAllByRole('button', { name: 'Close sidebar' })[0]);
    const chosenControl = document.activeElement;
    expect(chosenControl).not.toBe(document.body);
    expect(chosenControl).not.toBe(screen.getByTestId('chat-input'));
    mockConnectionStatus = 'connected';
    page.rerender(view());
    expect(chosenControl).toHaveFocus();
    expect(screen.getByTestId('chat-input')).toHaveValue('Keep my draft');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('persists a replay cursor with its applied messages across browser reload', () => {
    const originalScroll = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = vi.fn();
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
        { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
      ]));
      const page = renderPage();
      const appliedMessages = [{ id: 'reply', role: 'assistant' as const, content: 'Recovered', status: 'complete' as const, timestamp: 2 }];
      const cursor = `${'a'.repeat(32)}:9`;
      act(() => { vi.mocked(useAgUiEvents).mock.lastCall?.[0].onMessagesChange(ISSUED_ID, appliedMessages, cursor); });
      expect(storedConversations()[0]).toHaveProperty('replayCursor', cursor);
      expect(window.localStorage.getItem(STORAGE_KEY)).toContain('Recovered');
      page.unmount();
      renderPage();
      expect(vi.mocked(useAgUiEvents).mock.lastCall?.[0].conversation?.replayCursor).toBe(cursor);
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScroll;
    }
  });

  it('reports expired session health from the server rather than showing a live pod', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValue({ mode: 'persistent', health: 'recovering', sequence: 2 });
    renderPage();
    expect(await screen.findByRole('status', { name: 'Persistent session recovering' })).toHaveTextContent('recovering');
    expect(screen.queryByRole('status', { name: 'Persistent session active' })).not.toBeInTheDocument();
  });

  it('disables mode selection when backend state is unavailable', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockRejectedValue(new Error('gateway unavailable'));
    renderPage();
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Session mode unavailable'));
    expect(screen.getByRole('combobox', { name: 'Session runtime' })).toBeDisabled();
    expect(mockSelectChatMode).not.toHaveBeenCalled();
  });
  it('clears a stale active state when the backend becomes unavailable', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockReadChatMode.mockResolvedValueOnce({ mode: 'persistent', health: 'active', sequence: 1, idle_timeout_seconds: 300 });
    mockReadChatMode.mockRejectedValue(new Error('backend unavailable'));
    const page = renderPage();
    expect(await screen.findByRole('status', { name: 'Persistent session active' })).toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent('5 minutes');
    mockAwaitingReply = true;
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Session mode unavailable'));
    expect(screen.queryByRole('status', { name: 'Persistent session active' })).not.toBeInTheDocument();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Session runtime' })).toBeDisabled();
  });

  it('offers Stop for the active reply without enabling another send', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Active', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockAwaitingReply = true;
    renderPage();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Stop current reply' }));
    expect(mockCancelTurn).toHaveBeenCalledOnce();
    expect(screen.getByTestId('chat-input')).toBeDisabled();
  });

  it('shows an accepted stop as pending teardown rather than a finished reply', () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Active', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockAwaitingReply = true;
    mockCancelState = 'requested';
    renderPage();
    expect(screen.getByRole('button', { name: 'Stop current reply' })).toBeDisabled();
    expect(screen.getByText('Stop requested')).toBeInTheDocument();
    expect(screen.getByTestId('chat-input')).toBeDisabled();
  });

  it('shows a real tool call before streamed text and settles on the reported outcome', () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Active', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    mockAwaitingReply = true;
    mockTurnPhase = 'running';
    mockActiveToolCalls = [{ toolCallId: 'tool-1', toolCallName: 'WebSearch', args: '', status: 'running' }];
    const page = renderPage();
    expect(screen.getByTestId('turn-progress')).toHaveTextContent('Running');
    expect(screen.getByRole('group', { name: 'Tool call: WebSearch' })).toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Waiting for assistant update' })).toBeInTheDocument();
    mockAwaitingReply = false;
    mockTurnPhase = 'completed';
    mockActiveToolCalls = [];
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('turn-progress')).toHaveTextContent('Completed');
    expect(screen.queryByRole('group', { name: 'Tool call: WebSearch' })).not.toBeInTheDocument();
  });

  it('shows a running tool only once when the call is also attached to a message', () => {
    const toolCall: ToolCallInfo = { toolCallId: 'tool-1', toolCallName: 'WebSearch', args: '', status: 'running' };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Active', createdAt: 1, updatedAt: 1, messages: [
        { id: 'reply', role: 'assistant', content: 'Searching', status: 'streaming', timestamp: 2, toolCalls: [toolCall] },
      ] },
    ]));
    mockAwaitingReply = true;
    mockActiveToolCalls = [toolCall];
    renderPage();
    expect(screen.getAllByRole('group', { name: 'Tool call: WebSearch' })).toHaveLength(1);
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

  it('includes only the verified activity reference in contextual turns', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    renderPage('authorized-run');
    await userEvent.setup().type(screen.getByTestId('chat-input'), 'What happened?{Enter}');
    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledWith('Regarding my activity authorized-run: What happened?', undefined));
    expect(mockRequestId).not.toHaveBeenCalled();
  });

  it('carries only the verified installation reference into an existing conversation', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    renderPage(undefined, '101');
    await userEvent.setup().type(screen.getByTestId('chat-input'), 'What is deployed?{Enter}');
    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledWith('Regarding installation 101: What is deployed?', undefined));
    expect(mockRequestId).not.toHaveBeenCalled();
  });

  it('retains installation context while waiting for the first server-issued session', async () => {
    mockConnectionStatus = 'disconnected';
    mockRequestId.mockResolvedValue(ISSUED_ID);
    const page = renderPage(undefined, '101');
    await userEvent.setup().type(screen.getByTestId('chat-input'), 'What is deployed?{Enter}');
    await waitFor(() => expect(storedConversations()[0].id).toBe(ISSUED_ID));
    expect(mockSendMessage).not.toHaveBeenCalled();
    mockConnectionStatus = 'connected';
    page.rerender(<MemoryRouter><AgentChat installationId="101" /></MemoryRouter>);
    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledWith('Regarding installation 101: What is deployed?'));
    expect(mockSendMessage).toHaveBeenCalledTimes(1);
  });

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

  it('keeps a draft and offers transcript review after a worker failure without restarting the turn', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [
        { id: 'question', role: 'user', content: 'Original request', status: 'complete', timestamp: 1 },
      ] },
    ]));
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Follow-up draft');
    mockTurnPhase = 'failed';
    act(() => vi.mocked(useAgUiEvents).mock.lastCall?.[0].onMessagesChange(ISSUED_ID, [
      { id: 'question', role: 'user', content: 'Original request', status: 'complete', timestamp: 1 },
      { id: 'failure', role: 'assistant', content: '', status: 'error', errorReason: 'Worker stopped', timestamp: 2 },
    ]));
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByText('Error: Worker stopped')).toBeInTheDocument();
    expect(screen.getByTestId('chat-recovery')).toHaveTextContent('Check what arrived');
    expect(screen.getByRole('button', { name: 'Review conversation' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry connection' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Review conversation' }));
    expect(screen.getByRole('log', { name: 'Chat messages' })).toHaveFocus();
    expect(screen.getByTestId('chat-input')).toHaveValue('Follow-up draft');
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('preserves unsent input when a refused session must be replaced', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([
      { id: ISSUED_ID, title: 'Existing', createdAt: 1, updatedAt: 1, messages: [] },
    ]));
    const freshId = 'sess-99998888777766665555444433332222';
    mockRequestId.mockResolvedValue(freshId);
    const user = userEvent.setup();
    const page = renderPage();
    await user.type(screen.getByTestId('chat-input'), 'Still unsent');
    mockSessionExpired = true;
    mockTurnPhase = 'interrupted';
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.queryByTestId('chat-recovery')).not.toBeInTheDocument();
    expect(screen.getByTestId('session-expired-notice')).toHaveTextContent('unsent text is preserved');
    expect(screen.getByTestId('chat-input')).toHaveValue('Still unsent');
    expect(screen.getByTestId('chat-input')).toBeDisabled();
    await user.click(screen.getByTestId('session-expired-new'));
    await waitFor(() => expect(storedConversations()[0].id).toBe(freshId));
    mockSessionExpired = false;
    mockTurnPhase = null;
    page.rerender(<MemoryRouter><AgentChat /></MemoryRouter>);
    expect(screen.getByTestId('chat-input')).toHaveValue('Still unsent');
    expect(mockSendMessage).not.toHaveBeenCalled();
    expect(mockRequestId).toHaveBeenCalledTimes(1);
  });
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
