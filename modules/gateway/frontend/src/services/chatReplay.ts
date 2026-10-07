import { apiClient } from './api';

export type ChatReplayEvent = {
  sequence: number;
  event_id: string;
  kind: 'ag_ui' | 'terminal';
  payload: Record<string, unknown>;
  cursor: string;
};

export type ChatReplayPage = {
  status: 'ok' | 'history_refresh_required';
  reason?: 'retention_gap' | 'cursor_changed' | 'journal_unavailable';
  events: ChatReplayEvent[];
  cursor: string | null;
  has_more: boolean;
  latest_sequence?: number;
};

export function readChatReplay(sessionId: string, cursor: string | null): Promise<ChatReplayPage> {
  const query = new URLSearchParams({ limit: '100' });
  if (cursor) query.set('cursor', cursor);
  return apiClient.get<ChatReplayPage>(`/chat/sessions/${encodeURIComponent(sessionId)}/events?${query}`);
}
