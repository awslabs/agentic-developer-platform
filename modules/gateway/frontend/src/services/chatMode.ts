import { apiClient } from './api';

export type ChatSessionMode = 'ephemeral' | 'persistent';
/** Turn executors a persistent session may run (#8115); ephemeral sessions always run the sandbox loop. */
export type ChatExecutor = 'sandbox-loop' | 'claude-code' | 'kirocrew';
export type ChatSessionState = {
  mode: ChatSessionMode;
  health: 'idle' | 'active' | 'ending' | 'ended' | 'recovering' | 'cleanup_delayed';
  sequence: number;
  cleanup_elapsed_seconds?: number;
  idle_timeout_seconds?: number;
  pending_mode?: ChatSessionMode;
  /** The executor stored on the session; absent means the deployment default applies. */
  executor?: ChatExecutor;
};

export function readChatMode(sessionId: string): Promise<ChatSessionState> {
  return apiClient.get<ChatSessionState>(`/chat/sessions/${encodeURIComponent(sessionId)}/mode`);
}

export function selectChatMode(sessionId: string, mode: ChatSessionMode, executor?: ChatExecutor): Promise<ChatSessionState> {
  return apiClient.put<ChatSessionState>(`/chat/sessions/${encodeURIComponent(sessionId)}/mode`,
    executor === undefined ? { mode } : { mode, executor });
}

export function endChatSession(sessionId: string): Promise<ChatSessionState> {
  return apiClient.post<ChatSessionState>(`/chat/sessions/${encodeURIComponent(sessionId)}/end`);
}
