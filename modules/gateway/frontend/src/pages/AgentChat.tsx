/**
 * AgentChat — full-page chat with the agent via WebSocket.
 *
 * Issue #97 Phase 2 (L2): AG-UI event protocol. The hook handles both legacy
 * frames and AG-UI events during the backward-compat window.
 *
 * Layout: sidebar (conversation list) | main pane (messages + input).
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { useAgUiEvents } from '@/hooks/useAgUiEvents';
import { requestServerSessionId } from '@/services/chatSession';
import { ConversationSidebar } from '@/components/chat/ConversationSidebar';
import { ChatMessageRenderer } from '@/components/chat/ChatMessageRenderer';
import { ToolCallRow } from '@/components/chat/ToolCallRow';
import { SessionMetaPanel } from '@/components/chat/SessionMetaPanel';
import { DraftPanel } from '@/components/chat/DraftPanel';
import { TypingIndicator } from '@/components/chat/TypingIndicator';
import { FileDropZone, type PendingUpload } from '@/components/chat/FileDropZone';
import type { ChatMessage, Conversation, ConnectionStatus } from '@/types/chat';

// highlight.js theme for code blocks
import 'highlight.js/styles/github-dark.css';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'adp_chat_conversations';
const MAX_MESSAGE_LENGTH = 10_000;

const SUGGESTION_CHIPS = [
  'Explain this codebase',
  'Help me debug a build failure',
  'Write unit tests for a React component',
  'Summarize recent pull requests',
  'How do I deploy to production?',
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build the local record for a conversation the SERVER has already created.
 *
 * #5615: `sessionId` is issued by the server and passed in — this page no longer
 * invents one. The old `generateSessionId()` produced `sess-<clock>-<weak
 * random>`, which the server then adopted, so a conversation's name was the
 * browser's choice and was largely predictable. Creation now happens server-side
 * (`requestServerSessionId`), and nothing here may synthesise a fallback: a
 * locally invented id would be refused by the ingress and would strand the user
 * in a conversation that can never receive a reply.
 */
function createConversation(sessionId: string, title?: string): Conversation {
  const now = Date.now();
  return {
    id: sessionId,
    title: title || 'New conversation',
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

export default function AgentChat() {
  // Persisted conversations
  const [conversations, setConversations] = useLocalStorage<Conversation[]>(STORAGE_KEY, []);
  const [activeConvId, setActiveConvId] = useState<string | null>(() => {
    return conversations.length > 0 ? conversations[0].id : null;
  });

  const activeConversation = conversations.find((c) => c.id === activeConvId) ?? null;

  // Sidebar collapsed state for responsive
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // Input state
  const [inputValue, setInputValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // #5615: starting a conversation is now a server round-trip, so it can be in
  // flight and it can fail.
  const [isStartingConversation, setIsStartingConversation] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  // Guards against a second in-flight creation (double-click, or Enter while the
  // first request is still open). Without it each attempt would mint its own
  // server-side row and all but the last would be orphaned.
  const startInFlightRef = useRef(false);

  // ------------------------------------------------------------------
  // Conversation CRUD
  // ------------------------------------------------------------------

  const handleMessagesChange = useCallback(
    (sessionId: string, messages: ChatMessage[]) => {
      setConversations((prev) =>
        prev.map((c) => {
          if (c.id !== sessionId) return c;
          // Update title from first user message if still default.
          let title = c.title;
          if (title === 'New conversation') {
            const firstUser = messages.find((m) => m.role === 'user');
            if (firstUser) {
              title = firstUser.content.slice(0, 50) + (firstUser.content.length > 50 ? '...' : '');
            }
          }
          return { ...c, messages, title, updatedAt: Date.now() };
        }),
      );
    },
    [setConversations],
  );

  /**
   * Start a conversation: ask the server for its identifier, then record it.
   *
   * #5615. Two ordering rules matter here, and both are about not leaving debris:
   *
   *   - The sidebar entry is added only AFTER the server acknowledges an
   *     identifier. Adding it first and patching the id in later would put a
   *     conversation on screen that the server has never heard of, and a failed
   *     or lost reply would leave it there permanently as a dead row the user
   *     could click but never use.
   *   - A failure is reported, not papered over. There is deliberately no
   *     fallback to a locally invented identifier: the ingress refuses ids it
   *     did not issue, so a fallback would produce a conversation that silently
   *     swallows every message.
   *
   * Retrying is safe. Each attempt gets a fresh identifier and never rebinds an
   * earlier one; an identifier whose reply was lost in flight is an empty owned
   * row that the sessions table's TTL reaps.
   */
  const startConversation = useCallback(async (): Promise<string | null> => {
    if (startInFlightRef.current) return null;
    startInFlightRef.current = true;
    setIsStartingConversation(true);
    setStartError(null);
    try {
      const sessionId = await requestServerSessionId();
      const conv = createConversation(sessionId);
      setConversations((prev) => [conv, ...prev]);
      setActiveConvId(conv.id);
      return conv.id;
    } catch (err) {
      setStartError(
        err instanceof Error ? err.message : 'Could not start a conversation. Please try again.',
      );
      return null;
    } finally {
      startInFlightRef.current = false;
      setIsStartingConversation(false);
    }
  }, [setConversations]);

  const handleCreateConversation = useCallback(() => {
    void startConversation();
  }, [startConversation]);

  const handleSelectConversation = useCallback((id: string) => {
    setActiveConvId(id);
  }, []);

  const handleDeleteConversation = useCallback(
    (id: string) => {
      setConversations((prev) => prev.filter((c) => c.id !== id));
      if (activeConvId === id) {
        const remaining = conversations.filter((c) => c.id !== id);
        setActiveConvId(remaining.length > 0 ? remaining[0].id : null);
      }
    },
    [activeConvId, conversations, setConversations],
  );

  // ------------------------------------------------------------------
  // Agent chat hook
  // ------------------------------------------------------------------

  const { connectionStatus, isAwaitingReply, reconnectAttempt, sessionExpired, sessionMeta, sendMessage, activeToolCalls, wsRef } = useAgUiEvents({
    conversation: activeConversation,
    onMessagesChange: handleMessagesChange,
  });

  // Stage C (#186): pending file attachments for the next message
  const [pendingUploads, setPendingUploads] = useState<PendingUpload[]>([]);

  const handleUploadComplete = useCallback((upload: PendingUpload) => {
    setPendingUploads((prev) => [...prev, upload]);
  }, []);

  const handleRemoveUpload = useCallback((artifactId: string) => {
    setPendingUploads((prev) => prev.filter((u) => u.artifactId !== artifactId));
  }, []);

  // ------------------------------------------------------------------
  // Auto-scroll
  // ------------------------------------------------------------------

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [activeConversation?.messages]);

  // ------------------------------------------------------------------
  // Input handling
  // ------------------------------------------------------------------

  const handleSend = useCallback(() => {
    const text = inputValue.trim();
    if (!text || isAwaitingReply) return;

    // If no conversation, ask the server to start one first (#5615). The typed
    // text stays in the box; the effect below sends it once the socket for the
    // new conversation is connected. On failure the text is still there and the
    // error is shown, so the user loses nothing and can retry.
    if (!activeConvId) {
      void startConversation();
      return;
    }

    // Stage C (#186): include attachment IDs in the message
    const attachmentIds = pendingUploads.map((u) => u.artifactId);
    sendMessage(text, attachmentIds.length > 0 ? attachmentIds : undefined);
    setInputValue('');
    setPendingUploads([]);

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [inputValue, isAwaitingReply, activeConvId, sendMessage, startConversation, pendingUploads]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  // Auto-resize textarea
  const handleTextareaChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInputValue(e.target.value);
      // Auto-resize
      const el = e.target;
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    },
    [],
  );

  const handleSuggestionClick = useCallback(
    (chip: string) => {
      if (!activeConvId) {
        // #5615: park the chip in the input and let the connect effect send it
        // once the server has issued an identifier.
        setInputValue(chip);
        void startConversation();
        return;
      }
      sendMessage(chip);
    },
    [activeConvId, sendMessage, startConversation],
  );

  // Send pending input after connection establishes.
  // #5615: this is what delivers the first message of a conversation. The send
  // is deliberately gated on `activeConvId` — the SERVER-ISSUED id — so the
  // first message can only ever go to an identifier the server acknowledged.
  useEffect(() => {
    if (connectionStatus === 'connected' && inputValue.trim() && activeConvId && !sessionExpired) {
      const text = inputValue.trim();
      sendMessage(text);
      setInputValue('');
    }
    // Only trigger when connection status changes to connected
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionStatus]);

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  const messages = activeConversation?.messages ?? [];
  const charCount = inputValue.length;

  return (
    <div className="flex h-[calc(100vh-8rem)] rounded-xl overflow-hidden border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900">
      {/* Sidebar */}
      <div className={`${sidebarOpen ? 'w-72 flex-shrink-0' : 'w-0 overflow-hidden'} transition-all duration-200`}>
        <ConversationSidebar
          conversations={conversations}
          activeId={activeConvId}
          onSelect={handleSelectConversation}
          onCreate={handleCreateConversation}
          onDelete={handleDeleteConversation}
        />
      </div>

      {/* Main pane */}
      <div className="flex flex-col flex-1 min-w-0">
        {/* Top bar */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
          {/* Toggle sidebar */}
          <button
            onClick={() => setSidebarOpen((v) => !v)}
            className="p-1.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors lg:hidden"
            aria-label={sidebarOpen ? 'Close sidebar' : 'Open sidebar'}
          >
            <MenuIcon />
          </button>
          <button
            onClick={() => setSidebarOpen((v) => !v)}
            className="p-1.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors hidden lg:block"
            aria-label={sidebarOpen ? 'Close sidebar' : 'Open sidebar'}
          >
            <MenuIcon />
          </button>

          <h1 className="text-sm font-medium text-gray-700 dark:text-gray-300 truncate flex-1">
            {activeConversation?.title || 'Agent Chat'}
          </h1>

          {/* Connection indicator */}
          <ConnectionBadge status={connectionStatus} reconnectAttempt={reconnectAttempt} />
        </div>

        {/* Messages area */}
        <div
          className="flex-1 overflow-y-auto px-4 py-4"
          role="log"
          aria-live="polite"
          aria-label="Chat messages"
        >
          {messages.length === 0 ? (
            <EmptyState onChipClick={handleSuggestionClick} />
          ) : (
            <>
              {messages.map((msg) => (
                <div key={msg.id}>
                  <ChatMessageRenderer message={msg} />
                  {/* AG-UI tool calls rendered as collapsible rows below assistant messages */}
                  {msg.role === 'assistant' && msg.toolCalls && msg.toolCalls.length > 0 && (
                    <div className="ml-10 mb-2">
                      {msg.toolCalls.map((tc) => (
                        <ToolCallRow key={tc.toolCallId} toolCall={tc} />
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {/* Active tool calls for the current turn (not yet attached to a message) */}
              {activeToolCalls.length > 0 && (
                <div className="ml-10 mb-2">
                  {activeToolCalls
                    .filter((tc) => tc.status === 'running')
                    .map((tc) => (
                      <ToolCallRow key={tc.toolCallId} toolCall={tc} />
                    ))}
                </div>
              )}
              {/* Typing indicator — on from the moment the user presses send
                  until the assistant bubble is complete. Covers the pre-ACK
                  silent gap (Lambda classifying) and the post-ACK thinking
                  window where no tokens have streamed yet. */}
              {isAwaitingReply && (
                <div className="ml-10 mb-2" data-testid="typing-indicator">
                  <TypingIndicator
                    toolUse={
                      (() => {
                        const running = activeToolCalls.find((tc) => tc.status === 'running');
                        return running ? { tool_name: running.toolCallName } : null;
                      })()
                    }
                  />
                </div>
              )}
              <div ref={messagesEndRef} />
            </>
          )}
        </div>

        {/* Live intent draft (#4208) — populated by update_draft via STATE_DELTA */}
        <DraftPanel draft={sessionMeta?.draft} />

        {/* Session metadata (AG-UI STATE_DELTA) */}
        <SessionMetaPanel meta={sessionMeta} />

        {/* Input area with drag-drop */}
        <FileDropZone wsRef={wsRef} sessionId={activeConvId} onUploadComplete={handleUploadComplete}>
        <div className="border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
          {/* #5615: starting a conversation is a server round-trip, so it can
              fail. Say so and offer a retry rather than appearing to do nothing
              — the alternative (inventing an id locally) would produce a
              conversation the server refuses every message on. */}
          {startError && (
            <div
              className="flex items-center justify-between gap-3 mb-2 px-3 py-2 rounded-lg text-xs bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300"
              role="alert"
              data-testid="start-session-error"
            >
              <span>{startError}</span>
              <button
                onClick={handleCreateConversation}
                disabled={isStartingConversation}
                className="flex-shrink-0 font-medium underline hover:no-underline disabled:opacity-50"
                data-testid="start-session-retry"
              >
                Try again
              </button>
            </div>
          )}
          {/* #5615: the server refused this conversation's id. Commonly the row
              expired (24h TTL) while this browser kept the id in localStorage,
              so there is nothing to retry — only a new conversation to start. */}
          {sessionExpired && (
            <div
              className="flex items-center justify-between gap-3 mb-2 px-3 py-2 rounded-lg text-xs bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300"
              role="alert"
              data-testid="session-expired-notice"
            >
              <span>This conversation is no longer available.</span>
              <button
                onClick={handleCreateConversation}
                disabled={isStartingConversation}
                className="flex-shrink-0 font-medium underline hover:no-underline disabled:opacity-50"
                data-testid="session-expired-new"
              >
                Start a new conversation
              </button>
            </div>
          )}
          {/* Pending uploads chips */}
          {pendingUploads.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mb-2">
              {pendingUploads.map((u) => (
                <span
                  key={u.artifactId}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
                >
                  <PaperclipIcon />
                  {u.filename}
                  <button
                    onClick={() => handleRemoveUpload(u.artifactId)}
                    className="ml-0.5 hover:text-red-500"
                    aria-label={`Remove ${u.filename}`}
                  >
                    &times;
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
            <div className="flex-1 relative">
              <textarea
                ref={textareaRef}
                value={inputValue}
                onChange={handleTextareaChange}
                onKeyDown={handleKeyDown}
                placeholder={
                  sessionExpired
                    ? 'Start a new conversation to continue'
                    : isStartingConversation
                      ? 'Starting a conversation...'
                      : connectionStatus === 'connected'
                        ? 'Type a message... (Enter to send, Shift+Enter for newline)'
                        : connectionStatus === 'connecting' || connectionStatus === 'reconnecting'
                          ? 'Connecting...'
                          : 'Start a conversation to connect'
                }
                disabled={
                  isAwaitingReply ||
                  // #5615: no point typing into an id the server has refused, or
                  // while the id for a brand-new conversation is still in flight.
                  sessionExpired ||
                  isStartingConversation ||
                  connectionStatus === 'connecting' ||
                  connectionStatus === 'reconnecting'
                }
                maxLength={MAX_MESSAGE_LENGTH}
                rows={1}
                className="w-full resize-none rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-white px-4 py-2.5 pr-16 text-sm placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-500 disabled:opacity-50 disabled:cursor-not-allowed"
                aria-label="Message input"
                data-testid="chat-input"
              />
              {/* Char counter */}
              {charCount > 0 && (
                <span className="absolute bottom-2 right-14 text-xs text-gray-400">
                  {charCount.toLocaleString()}/{MAX_MESSAGE_LENGTH.toLocaleString()}
                </span>
              )}
            </div>

            <button
              onClick={handleSend}
              disabled={
                !inputValue.trim() ||
                isAwaitingReply ||
                sessionExpired ||
                isStartingConversation ||
                (connectionStatus !== 'connected' && !!activeConvId)
              }
              className="flex-shrink-0 p-2.5 rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              aria-label="Send message"
              data-testid="send-button"
            >
              <SendIcon />
            </button>
          </div>
        </div>
        </FileDropZone>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function EmptyState({ onChipClick }: { onChipClick: (chip: string) => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-4">
      <div className="w-16 h-16 rounded-full bg-primary-100 dark:bg-primary-900/30 flex items-center justify-center mb-4">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-primary-600 dark:text-primary-400" aria-hidden="true">
          <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </div>
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">
        Start a conversation
      </h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mb-6 max-w-sm">
        Chat with the agent to get help with your codebase, debug issues, write tests, and more.
      </p>

      {/* Suggestion chips */}
      <div className="flex flex-wrap gap-2 justify-center max-w-lg" role="list" aria-label="Suggested prompts">
        {SUGGESTION_CHIPS.map((chip) => (
          <button
            key={chip}
            onClick={() => onChipClick(chip)}
            className="px-3 py-1.5 rounded-full text-sm border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
            role="listitem"
          >
            {chip}
          </button>
        ))}
      </div>
    </div>
  );
}

function ConnectionBadge({
  status,
  reconnectAttempt,
}: {
  status: ConnectionStatus;
  reconnectAttempt: number;
}) {
  const config: Record<ConnectionStatus, { color: string; label: string }> = {
    connected: { color: 'bg-green-500', label: 'Connected' },
    connecting: { color: 'bg-yellow-500 animate-pulse', label: 'Connecting...' },
    reconnecting: {
      color: 'bg-yellow-500 animate-pulse',
      label: `Reconnecting (${reconnectAttempt})...`,
    },
    disconnected: { color: 'bg-gray-400', label: 'Disconnected' },
  };

  const { color, label } = config[status];

  return (
    <div className="flex items-center gap-1.5" aria-label={`Connection status: ${label}`}>
      <span className={`w-2 h-2 rounded-full ${color}`} aria-hidden="true" />
      <span className="text-xs text-gray-500 dark:text-gray-400 hidden sm:inline">{label}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

function MenuIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
    </svg>
  );
}

function PaperclipIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  );
}
