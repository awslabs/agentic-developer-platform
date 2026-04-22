/**
 * AgentChat — full-page chat with the agent via WebSocket.
 *
 * Issue #97 Phase 1 (L1): raw WS frames, markdown rendering, auto-reconnect,
 * localStorage-persisted conversation threads.
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
import { useAgentChat } from '@/hooks/useAgentChat';
import { ConversationSidebar } from '@/components/chat/ConversationSidebar';
import { ChatMessageRenderer } from '@/components/chat/ChatMessageRenderer';
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

function generateSessionId(): string {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function createConversation(title?: string): Conversation {
  const now = Date.now();
  return {
    id: generateSessionId(),
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

  const handleCreateConversation = useCallback(() => {
    const conv = createConversation();
    setConversations((prev) => [conv, ...prev]);
    setActiveConvId(conv.id);
  }, [setConversations]);

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

  const { connectionStatus, isAwaitingReply, reconnectAttempt, sendMessage } = useAgentChat({
    conversation: activeConversation,
    onMessagesChange: handleMessagesChange,
  });

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

    // If no conversation, create one first.
    if (!activeConvId) {
      const conv = createConversation();
      setConversations((prev) => [conv, ...prev]);
      setActiveConvId(conv.id);
      // We can't send yet because the hook needs to connect first.
      // Store the text and send after effect.
      return;
    }

    sendMessage(text);
    setInputValue('');

    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [inputValue, isAwaitingReply, activeConvId, sendMessage, setConversations]);

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
        const conv = createConversation();
        setConversations((prev) => [conv, ...prev]);
        setActiveConvId(conv.id);
        setInputValue(chip);
        return;
      }
      sendMessage(chip);
    },
    [activeConvId, sendMessage, setConversations],
  );

  // Send pending input after connection establishes
  useEffect(() => {
    if (connectionStatus === 'connected' && inputValue.trim() && activeConvId) {
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
                <ChatMessageRenderer key={msg.id} message={msg} />
              ))}
              <div ref={messagesEndRef} />
            </>
          )}
        </div>

        {/* Input area */}
        <div className="border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
          <div className="flex items-end gap-2">
            <div className="flex-1 relative">
              <textarea
                ref={textareaRef}
                value={inputValue}
                onChange={handleTextareaChange}
                onKeyDown={handleKeyDown}
                placeholder={
                  connectionStatus === 'connected'
                    ? 'Type a message... (Enter to send, Shift+Enter for newline)'
                    : connectionStatus === 'connecting' || connectionStatus === 'reconnecting'
                      ? 'Connecting...'
                      : 'Start a conversation to connect'
                }
                disabled={
                  isAwaitingReply ||
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

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  );
}
