/**
 * My Chats page component.
 *
 * Issue #179: Allows logged-in users to view their conversation history
 * through the gateway.
 */

import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Input } from '@/components/ui';
import { TableSkeleton } from '@/components/LoadingScreen';
import { getMyChats, getChatDetail } from '@/services/chats';
import type { ChatSummary, ChatDetail, ChatFilters } from '@/services/chats';
import { formatCurrency, formatNumber, formatDate } from '@/utils/format';

// Modal for viewing chat details
function ChatDetailModal({
  chat,
  onClose,
}: {
  chat: ChatDetail | null;
  onClose: () => void;
}) {
  if (!chat) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex items-center justify-center min-h-screen px-4">
        {/* Backdrop */}
        <div
          className="fixed inset-0 bg-black bg-opacity-50 transition-opacity"
          onClick={onClose}
        />

        {/* Modal */}
        <div className="relative bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full max-h-[80vh] overflow-hidden">
          {/* Header */}
          <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                Chat Details
              </h2>
              <button
                onClick={onClose}
                className="text-gray-400 hover:text-gray-500"
              >
                <span className="sr-only">Close</span>
                <svg
                  className="h-6 w-6"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M6 18L18 6M6 6l12 12"
                  />
                </svg>
              </button>
            </div>
          </div>

          {/* Content */}
          <div className="px-6 py-4 overflow-y-auto max-h-[60vh]">
            <dl className="grid grid-cols-2 gap-4">
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Request ID
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white font-mono">
                  {chat.requestId}
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Timestamp
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white">
                  {formatDate(chat.timestamp)}
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Model
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white">
                  {chat.model}
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Status
                </dt>
                <dd className="mt-1">
                  <span
                    className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                      chat.statusCode === 200
                        ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100'
                        : 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-100'
                    }`}
                  >
                    {chat.statusCode}
                  </span>
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Input Tokens
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white">
                  {formatNumber(chat.inputTokens)}
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Output Tokens
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white">
                  {formatNumber(chat.outputTokens)}
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Cost
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white">
                  {formatCurrency(chat.costUsd)}
                </dd>
              </div>
              <div>
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Latency
                </dt>
                <dd className="mt-1 text-sm text-gray-900 dark:text-white">
                  {formatNumber(chat.latencyMs)} ms
                </dd>
              </div>
            </dl>

            {/* Chat content (if available) */}
            {chat.chatLoggingAvailable ? (
              <div className="mt-6">
                <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">
                  Conversation
                </h3>
                {chat.requestMessages && (
                  <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
                    <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
                      Request
                    </h4>
                    <pre className="text-sm text-gray-900 dark:text-white whitespace-pre-wrap overflow-x-auto">
                      {JSON.stringify(chat.requestMessages, null, 2)}
                    </pre>
                  </div>
                )}
                {chat.responseContent && (
                  <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-4">
                    <h4 className="text-xs font-medium text-gray-500 uppercase mb-2">
                      Response
                    </h4>
                    <pre className="text-sm text-gray-900 dark:text-white whitespace-pre-wrap overflow-x-auto">
                      {chat.responseContent}
                    </pre>
                  </div>
                )}
              </div>
            ) : (
              <div className="mt-6 p-4 bg-gray-50 dark:bg-gray-900 rounded-lg">
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  💡 Full conversation content is not available. Enable chat
                  logging to see the full conversation history.
                </p>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end">
            <Button onClick={onClose}>Close</Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function MyChats() {
  // State for filters and pagination
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<ChatFilters>({});
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);

  // Fetch chat list
  const {
    data: chatsData,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['myChats', page, filters],
    queryFn: () => getMyChats(page, 20, filters),
  });

  // Fetch selected chat detail
  const { data: selectedChat } = useQuery({
    queryKey: ['chatDetail', selectedChatId],
    queryFn: () => (selectedChatId ? getChatDetail(selectedChatId) : null),
    enabled: !!selectedChatId,
  });

  // Handle filter changes
  const handleModelFilterChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setFilters((prev) => ({ ...prev, model: e.target.value || undefined }));
      setPage(1);
    },
    []
  );

  const handleStartDateChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setFilters((prev) => ({ ...prev, startDate: e.target.value || undefined }));
      setPage(1);
    },
    []
  );

  const handleEndDateChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setFilters((prev) => ({ ...prev, endDate: e.target.value || undefined }));
      setPage(1);
    },
    []
  );

  // Handle chat selection
  const handleViewChat = useCallback((chatId: string) => {
    setSelectedChatId(chatId);
  }, []);

  const handleCloseModal = useCallback(() => {
    setSelectedChatId(null);
  }, []);

  if (error) {
    return (
      <Alert variant="error" title="Error loading chats">
        {error instanceof Error ? error.message : 'Failed to load chat history'}
      </Alert>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          My Chats
        </h1>
        <p className="text-gray-500 dark:text-gray-400 mt-1">
          View your conversation history
        </p>
      </div>

      {/* Filters */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Model
            </label>
            <Input
              type="text"
              placeholder="Filter by model..."
              value={filters.model || ''}
              onChange={handleModelFilterChange}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Start Date
            </label>
            <Input
              type="date"
              value={filters.startDate || ''}
              onChange={handleStartDateChange}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              End Date
            </label>
            <Input
              type="date"
              value={filters.endDate || ''}
              onChange={handleEndDateChange}
            />
          </div>
        </div>
      </div>

      {/* Chat list */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        {isLoading ? (
          <TableSkeleton rows={10} />
        ) : chatsData && chatsData.items.length > 0 ? (
          <>
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-900">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Timestamp
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Model
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Tokens
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Cost
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                {chatsData.items.map((chat: ChatSummary) => (
                  <tr
                    key={chat.requestId}
                    className="hover:bg-gray-50 dark:hover:bg-gray-700"
                  >
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-white">
                      {formatDate(chat.timestamp)}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                      <span className="font-mono text-xs">{chat.model}</span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400 text-right">
                      <span className="text-blue-600 dark:text-blue-400">
                        {formatNumber(chat.inputTokens)}
                      </span>
                      {' / '}
                      <span className="text-green-600 dark:text-green-400">
                        {formatNumber(chat.outputTokens)}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400 text-right">
                      {formatCurrency(chat.costUsd)}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleViewChat(chat.requestId)}
                      >
                        View
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {/* Pagination */}
            <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
              <div className="text-sm text-gray-500 dark:text-gray-400">
                Showing{' '}
                {Math.min((page - 1) * 20 + 1, chatsData.total)} to{' '}
                {Math.min(page * 20, chatsData.total)} of{' '}
                {chatsData.total} chats
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 1}
                  onClick={() => setPage((p) => p - 1)}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!chatsData.hasMore}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        ) : (
          <div className="text-center py-12">
            <p className="text-gray-500 dark:text-gray-400">
              No chats found. Start using the platform to see your
              conversation history here.
            </p>
          </div>
        )}
      </div>

      {/* Chat detail modal */}
      {selectedChatId && (
        <ChatDetailModal chat={selectedChat || null} onClose={handleCloseModal} />
      )}
    </div>
  );
}
