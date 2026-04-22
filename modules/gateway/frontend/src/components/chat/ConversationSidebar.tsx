/**
 * ConversationSidebar — list of conversation threads with create/select/delete.
 *
 * Issue #97 Phase 1.
 */

import { useCallback, useState } from 'react';
import type { Conversation } from '@/types/chat';

interface ConversationSidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
}

export function ConversationSidebar({
  conversations,
  activeId,
  onSelect,
  onCreate,
  onDelete,
}: ConversationSidebarProps) {
  return (
    <aside
      className="flex flex-col h-full border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900"
      aria-label="Conversations"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
          Conversations
        </h2>
        <button
          onClick={onCreate}
          className="p-1.5 rounded-lg text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
          aria-label="New conversation"
          data-testid="new-conversation-button"
        >
          <PlusIcon />
        </button>
      </div>

      {/* Thread list */}
      <nav className="flex-1 overflow-y-auto" aria-label="Conversation list">
        {conversations.length === 0 ? (
          <p className="px-4 py-8 text-sm text-center text-gray-400 dark:text-gray-500">
            No conversations yet
          </p>
        ) : (
          <ul role="listbox" aria-label="Conversations">
            {conversations.map((conv) => (
              <ConversationItem
                key={conv.id}
                conversation={conv}
                isActive={conv.id === activeId}
                onSelect={onSelect}
                onDelete={onDelete}
              />
            ))}
          </ul>
        )}
      </nav>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Conversation item
// ---------------------------------------------------------------------------

interface ConversationItemProps {
  conversation: Conversation;
  isActive: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

function ConversationItem({ conversation, isActive, onSelect, onDelete }: ConversationItemProps) {
  const [showDelete, setShowDelete] = useState(false);

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onDelete(conversation.id);
    },
    [conversation.id, onDelete],
  );

  const lastMessage = conversation.messages[conversation.messages.length - 1];
  const preview = lastMessage
    ? lastMessage.content.slice(0, 60) + (lastMessage.content.length > 60 ? '...' : '')
    : 'Empty conversation';

  return (
    <li
      role="option"
      aria-selected={isActive}
      className={`
        relative cursor-pointer px-4 py-3 border-b border-gray-100 dark:border-gray-800
        transition-colors
        ${
          isActive
            ? 'bg-primary-50 dark:bg-primary-900/20 border-l-2 border-l-primary-600'
            : 'hover:bg-gray-100 dark:hover:bg-gray-800'
        }
      `}
      onClick={() => onSelect(conversation.id)}
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
      onFocus={() => setShowDelete(true)}
      onBlur={() => setShowDelete(false)}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect(conversation.id);
        }
      }}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-900 dark:text-white truncate">
            {conversation.title}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
            {preview}
          </p>
        </div>

        {showDelete && (
          <button
            onClick={handleDelete}
            className="p-1 rounded text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors flex-shrink-0"
            aria-label={`Delete conversation: ${conversation.title}`}
            data-testid="delete-conversation-button"
          >
            <TrashIcon />
          </button>
        )}
      </div>

      <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
        {formatRelativeTime(conversation.updatedAt)}
      </p>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatRelativeTime(ts: number): string {
  const diff = Date.now() - ts;
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'Just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function PlusIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
    </svg>
  );
}
