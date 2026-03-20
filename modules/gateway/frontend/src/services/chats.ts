/**
 * Chat history service for the "My Chats" feature.
 *
 * Issue #179: Allows logged-in users to view their conversation history
 * through the gateway.
 */

import { apiClient, buildQueryString } from './api';
import type { PaginatedResponse } from '@/types/api';

// Chat summary for list view
export interface ChatSummary {
  requestId: string;
  timestamp: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  firstMessagePreview: string | null;
  stopReason: string | null;
}

// Full chat detail
export interface ChatDetail {
  requestId: string;
  timestamp: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  latencyMs: number;
  statusCode: number;
  stopReason: string | null;
  requestMessages: Array<Record<string, unknown>> | null;
  responseContent: string | null;
  chatLoggingAvailable: boolean;
}

// Filter options for chat list
export interface ChatFilters {
  model?: string;
  startDate?: string;
  endDate?: string;
}

// Sort options for chat list
export type ChatSortBy = 'newest' | 'oldest' | 'cost' | 'tokens';

/**
 * Get the current user's chat history.
 *
 * @param page - Page number (1-indexed)
 * @param limit - Items per page
 * @param filters - Optional filters (model, date range)
 * @returns Paginated list of chat summaries
 */
export async function getMyChats(
  page: number = 1,
  limit: number = 20,
  filters?: ChatFilters
): Promise<PaginatedResponse<ChatSummary>> {
  const query = buildQueryString({
    page,
    limit,
    model: filters?.model,
    start_date: filters?.startDate,
    end_date: filters?.endDate,
  });

  const response = await apiClient.get<{
    chats: Array<{
      request_id: string;
      timestamp: string;
      model: string;
      input_tokens: number;
      output_tokens: number;
      cost_usd: number;
      first_message_preview: string | null;
      stop_reason: string | null;
    }>;
    total: number;
    page: number;
    limit: number;
  }>(`/admin/users/me/chats${query}`);

  return {
    items: response.chats.map(transformChatSummary),
    total: response.total,
    page: response.page,
    pageSize: response.limit,
    hasMore: response.page * response.limit < response.total,
  };
}

/**
 * Get details of a specific chat.
 *
 * @param requestId - The request ID of the chat
 * @returns Full chat details
 */
export async function getChatDetail(requestId: string): Promise<ChatDetail> {
  const response = await apiClient.get<{
    request_id: string;
    timestamp: string;
    model: string;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
    latency_ms: number;
    status_code: number;
    stop_reason: string | null;
    request_messages: Array<Record<string, unknown>> | null;
    response_content: string | null;
    chat_logging_available: boolean;
  }>(`/admin/users/me/chats/${requestId}`);

  return transformChatDetail(response);
}

/**
 * Get available user roles.
 *
 * Issue #179: Static list for the admin UI.
 */
export async function getAvailableRoles(): Promise<string[]> {
  const response = await apiClient.get<{ roles: string[] }>('/admin/users/roles');
  return response.roles;
}

// Transform functions
function transformChatSummary(data: {
  request_id: string;
  timestamp: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  first_message_preview: string | null;
  stop_reason: string | null;
}): ChatSummary {
  return {
    requestId: data.request_id,
    timestamp: data.timestamp,
    model: data.model,
    inputTokens: data.input_tokens,
    outputTokens: data.output_tokens,
    costUsd: data.cost_usd,
    firstMessagePreview: data.first_message_preview,
    stopReason: data.stop_reason,
  };
}

function transformChatDetail(data: {
  request_id: string;
  timestamp: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_ms: number;
  status_code: number;
  stop_reason: string | null;
  request_messages: Array<Record<string, unknown>> | null;
  response_content: string | null;
  chat_logging_available: boolean;
}): ChatDetail {
  return {
    requestId: data.request_id,
    timestamp: data.timestamp,
    model: data.model,
    inputTokens: data.input_tokens,
    outputTokens: data.output_tokens,
    costUsd: data.cost_usd,
    latencyMs: data.latency_ms,
    statusCode: data.status_code,
    stopReason: data.stop_reason,
    requestMessages: data.request_messages,
    responseContent: data.response_content,
    chatLoggingAvailable: data.chat_logging_available,
  };
}
