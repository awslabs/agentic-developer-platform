import { apiClient } from './api';

export type ChatSessionMode = 'ephemeral' | 'persistent';
export type ChatSessionState = {
  mode: ChatSessionMode;
  health: 'idle' | 'active' | 'ending' | 'ended' | 'recovering' | 'cleanup_delayed';
  sequence: number;
  cleanup_elapsed_seconds?: number;
  pending_mode?: ChatSessionMode;
};

export function readChatMode(sessionId: string): Promise<ChatSessionState> {
  return apiClient.get<ChatSessionState>(`/chat/sessions/${encodeURIComponent(sessionId)}/mode`);
}

export function selectChatMode(sessionId: string, mode: ChatSessionMode): Promise<ChatSessionState> {
  return apiClient.put<ChatSessionState>(`/chat/sessions/${encodeURIComponent(sessionId)}/mode`, { mode });
}

export function endChatSession(sessionId: string): Promise<ChatSessionState> {
  return apiClient.post<ChatSessionState>(`/chat/sessions/${encodeURIComponent(sessionId)}/end`);
}
