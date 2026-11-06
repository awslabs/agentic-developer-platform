/**
 * My conversations (#9026 / #9027): one entry per logical conversation across harnesses and origins.
 */
import { apiClient } from './api';

export type Harness = 'claude_code' | 'codex_cli' | 'kiro_crew' | 'web_chat' | 'cursor' | 'sdk' | 'kimi_code';
export type Origin = 'dev_box' | 'direct';
/** Where the entry comes from: the gateway's request records, or the Dev Box's 15-minute backup (#9069). */
export type ConversationSource = 'gateway' | 'backup';

export interface ConversationSummary {
  harness: Harness | string | null;
  conversation_id: string;
  untracked: boolean;
  origin: Origin | string | null;
  title: string;
  started_at: string | null;
  last_activity_at: string | null;
  turns: number;
  models: string[];
  cost_usd: number;
  source?: ConversationSource;
  /** Backup entries: gateway requests folded into this conversation. */
  requests?: number;
  /** Codex backups hold the owner's prompts only. */
  partial?: boolean;
  unreadable?: boolean;
}

export interface ConversationPage { conversations: ConversationSummary[]; next_before: string | null }

export interface ConversationTurn {
  role: string; time: string | null; text: string; collapsed: boolean; model?: string;
  input_tokens?: number | null; output_tokens?: number | null; cost_usd?: number;
}
export type ContentReason = 'ok' | 'not_logged' | 'not_retained' | 'unreadable' | 'partial';
export interface ConversationTurns {
  turns: ConversationTurn[]; content_available: boolean; content_reason?: ContentReason; request_count: number; latest_request_id: string | null;
  source?: ConversationSource; has_more?: boolean;
}

export const CONTENT_REASON_TEXT: Record<ContentReason, string> = {
  ok: '',
  not_logged: 'The gateway kept no log for these requests. Showing one entry per request.',
  not_retained: 'The gateway kept a log but not its messages (requests this large are not retained by the scrubber). Showing one entry per request.',
  unreadable: 'The stored log could not be read right now. Showing one entry per request.',
  partial: 'From your Dev Box backup: your prompts are stored, Codex replies are not yet.',
};

export const HARNESS_LABEL: Record<string, string> = {
  claude_code: 'Claude Code', codex_cli: 'Codex', kiro_crew: 'Kiro Crew', web_chat: 'Web chat', cursor: 'Cursor', sdk: 'SDK', kimi_code: 'Kimi Code',
};
export const ORIGIN_LABEL: Record<string, string> = { dev_box: 'Dev Box', direct: 'Direct' };

export function harnessLabel(harness: string | null): string { return harness ? HARNESS_LABEL[harness] ?? harness : 'Unknown harness'; }
export function originLabel(origin: string | null): string { return origin ? ORIGIN_LABEL[origin] ?? origin : 'Unknown'; }

export function listConversations(params: { limit?: number; before?: string | null; harness?: string; origin?: string }, signal?: AbortSignal) {
  const query = new URLSearchParams();
  query.set('limit', String(params.limit ?? 50));
  if (params.before) query.set('before', params.before);
  if (params.harness) query.set('harness', params.harness);
  if (params.origin) query.set('origin', params.origin);
  return apiClient.get<ConversationPage>(`/v1/me/conversations?${query.toString()}`, signal);
}

export function getConversationTurns(
  harness: string, conversationId: string, options: { source?: ConversationSource; offset?: number } = {}, signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  if (options.source === 'backup') query.set('source', 'backup');
  if (options.offset) query.set('offset', String(options.offset));
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return apiClient.get<ConversationTurns>(
    `/v1/me/conversations/${encodeURIComponent(harness)}/${encodeURIComponent(conversationId)}/turns${suffix}`, signal,
  );
}

/** "3 turns" for a backup entry (its stored dialogue), "3 requests" for a gateway entry (model calls). */
export function countLabel(chat: Pick<ConversationSummary, 'turns' | 'source'>): string {
  const noun = chat.source === 'backup' ? 'turn' : 'request';
  return `${chat.turns} ${noun}${chat.turns === 1 ? '' : 's'}`;
}
