/**
 * My conversations (#9027, epic #9023).
 *
 * One list, newest activity first, one row per logical conversation wherever it ran — Claude Code or
 * Codex in a tmux session on the Dev Box, the same tools on a laptop, Kiro Crew on the box, the web
 * chat — each labelled with its harness and origin. Replaces the per-request "My Chats" ledger and the
 * Dev Box backup-conversations page. Read-only: content comes from the gateway's scrubbed chat logs, or
 * from the Dev Box's 15-minute backup for conversations held on the box (#9069).
 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Badge, Button, Spinner } from '@/components/ui';
import Panel from '@/components/devbox/Panel';
import { CONTENT_REASON_TEXT, HARNESS_LABEL, ORIGIN_LABEL, countLabel, getConversationTurns, harnessLabel, listConversations, originLabel,
  type ConversationSummary, type ConversationTurns } from '@/services/conversations';

const PAGE = 50;
const TITLE_CHARS = 110;
const when = (value: string | null) => value ? new Date(value).toLocaleString() : 'Unknown';
const originVariant = (origin: string | null) => origin === 'dev_box' ? 'info' : 'default';

/** "just now", "12 min ago", "3 h ago", else the local date — with the exact time in the tooltip. */
export function relative(value: string | null, now = Date.now()): string {
  if (!value) return 'Unknown';
  const seconds = Math.max(0, Math.round((now - new Date(value).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 7 * 86_400) return `${Math.round(seconds / 86_400)} d ago`;
  return new Date(value).toLocaleDateString();
}

/** Day header for grouping: Today, Yesterday, else the local date. */
export function dayLabel(value: string | null, now = new Date()): string {
  if (!value) return 'Unknown day';
  const date = new Date(value);
  const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
  if (sameDay(date, now)) return 'Today';
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (sameDay(date, yesterday)) return 'Yesterday';
  return date.toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
}

const clamp = (title: string) => title.length > TITLE_CHARS ? `${title.slice(0, TITLE_CHARS).trimEnd()}…` : title;
const money = (value: number) => value >= 1 ? `$${value.toFixed(2)}` : `$${value.toFixed(3)}`;

export default function MyConversations() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [listError, setListError] = useState(false);
  const [harness, setHarness] = useState('');
  const [origin, setOrigin] = useState('');
  const [selected, setSelected] = useState<ConversationSummary | null>(null);
  const [turns, setTurns] = useState<ConversationTurns | null>(null);
  const [turnsError, setTurnsError] = useState(false);
  const [loadingTurns, setLoadingTurns] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    listConversations({ limit: PAGE, harness: harness || undefined, origin: origin || undefined }, controller.signal)
      .then(page => { setConversations(page.conversations); setNextBefore(page.next_before); setListError(false); })
      .catch(() => { if (!controller.signal.aborted) setListError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [harness, origin, reload]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setTurns(null);
    setTurnsError(false);
    getConversationTurns(selected.harness ?? '', selected.conversation_id, { source: selected.source }, controller.signal)
      .then(result => setTurns(result))
      .catch(() => { if (!controller.signal.aborted) setTurnsError(true); });
    return () => controller.abort();
  }, [selected]);

  const loadMoreTurns = useCallback(async () => {
    if (!selected || !turns?.has_more) return;
    setLoadingTurns(true);
    try {
      const more = await getConversationTurns(selected.harness ?? '', selected.conversation_id, { source: selected.source, offset: turns.turns.length });
      setTurns({ ...more, turns: [...turns.turns, ...more.turns] });
    } catch { setTurnsError(true); } finally { setLoadingTurns(false); }
  }, [selected, turns]);

  const loadMore = useCallback(async () => {
    if (!nextBefore) return;
    setLoadingMore(true);
    try {
      const page = await listConversations({ limit: PAGE, before: nextBefore, harness: harness || undefined, origin: origin || undefined });
      setConversations(previous => [...previous, ...page.conversations]);
      setNextBefore(page.next_before);
    } catch { setListError(true); } finally { setLoadingMore(false); }
  }, [nextBefore, harness, origin]);

  return <main className="mx-auto max-w-3xl px-4 py-8">
    <div className="mb-8">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">My conversations</h1>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Everything you said to a model through ADP, newest first — Claude Code, Codex, Kiro Crew or the web chat, from your Dev Box or directly.
        Read-only; conversations held on your Dev Box come from its backup, the rest from the gateway&apos;s redacted request logs.
      </p>
    </div>

    <div className="space-y-6">
      <Panel label="Conversations" title="Conversations"
        actions={<div className="flex gap-2 text-sm">
          <label className="text-gray-700 dark:text-gray-300">Harness{' '}
            <select aria-label="Harness" className="rounded border border-gray-300 bg-white p-1 dark:border-gray-600 dark:bg-gray-800" value={harness}
              onChange={event => { setSelected(null); setHarness(event.target.value); }}>
              <option value="">All</option>
              {Object.entries(HARNESS_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
          <label className="text-gray-700 dark:text-gray-300">Origin{' '}
            <select aria-label="Origin" className="rounded border border-gray-300 bg-white p-1 dark:border-gray-600 dark:bg-gray-800" value={origin}
              onChange={event => { setSelected(null); setOrigin(event.target.value); }}>
              <option value="">All</option>
              {Object.entries(ORIGIN_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
        </div>}>
        {listError && <div className="mt-4"><Alert variant="error" title="Could not load your conversations">
          <Button type="button" size="sm" variant="outline" onClick={() => setReload(value => value + 1)}>Retry</Button>
        </Alert></div>}
        {loading && !listError && <p role="status" className="mt-4 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400"><Spinner size="sm" /> Loading…</p>}
        {!loading && !listError && conversations.length === 0 && <p className="mt-4 text-sm text-gray-600 dark:text-gray-300">
          No conversations yet. Conversations appear here from their first turn once the gateway records them; nothing before that is grouped.</p>}
        {!loading && conversations.length > 0 && <ul aria-label="My conversations" className="mt-4 divide-y divide-gray-200 dark:divide-gray-700">
          {conversations.map((chat, index) => {
            const day = dayLabel(chat.last_activity_at);
            const newDay = index === 0 || dayLabel(conversations[index - 1].last_activity_at) !== day;
            return <li key={`${chat.harness}:${chat.conversation_id}`} className="py-3" data-day={day}>
              {newDay && <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400" role="heading" aria-level={3}>{day}</p>}
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" title={chat.title} className="text-left font-medium text-blue-600 underline dark:text-blue-400" onClick={() => setSelected(chat)}>{clamp(chat.title)}</button>
                <Badge size="sm" variant="default">{harnessLabel(chat.harness)}</Badge>
                <Badge size="sm" variant={originVariant(chat.origin)}>{originLabel(chat.origin)}</Badge>
                {chat.untracked && <Badge size="sm" variant="warning">untracked</Badge>}
              </div>
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                <time dateTime={chat.last_activity_at ?? undefined} title={when(chat.last_activity_at)}>{relative(chat.last_activity_at)}</time>
                {' · '}{countLabel(chat)}
                {chat.models.length > 0 && <> · {chat.models.slice(0, 2).join(', ')}{chat.models.length > 2 ? ` +${chat.models.length - 2}` : ''}</>}
                {' · '}{money(chat.cost_usd)}
              </p>
            </li>;
          })}
        </ul>}
        {nextBefore && !loading && <Button type="button" variant="outline" className="mt-4" disabled={loadingMore} onClick={() => void loadMore()}>
          {loadingMore ? 'Loading…' : 'Load older conversations'}</Button>}
      </Panel>

      {selected && <Panel label="Conversation detail" title={selected.title}
        description={`${harnessLabel(selected.harness)} · ${originLabel(selected.origin)} · started ${when(selected.started_at)} · ${countLabel(selected)} · ${money(selected.cost_usd)}`}
        actions={<Button type="button" size="sm" variant="outline" onClick={() => setSelected(null)}>Close</Button>}>
        {turnsError && <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-300">Could not load this conversation.</p>}
        {!turns && !turnsError && <p role="status" className="mt-4 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400"><Spinner size="sm" /> Loading turns…</p>}
        {turns && (!turns.content_available || turns.content_reason === 'partial') && <p role="note" className="mt-4 text-sm text-amber-700 dark:text-amber-300">
          {!turns.content_available && 'No stored content for this conversation. '}{CONTENT_REASON_TEXT[turns.content_reason ?? 'not_logged']}</p>}
        {turns && <ol aria-label="Conversation turns" className="mt-4 space-y-3">
          {turns.turns.map((turn, index) => <li key={`${index}:${turn.time ?? ''}`} className="rounded border border-gray-200 p-3 text-sm dark:border-gray-700">
            <p className="text-gray-500 dark:text-gray-400"><strong className="text-gray-900 dark:text-gray-100">{turn.role}</strong>
              {turn.time && <> · <time dateTime={turn.time}>{when(turn.time)}</time></>}{turn.model && <> · {turn.model}</>}
              {turn.role === 'request' && <> · {(turn.input_tokens ?? 0).toLocaleString()} in / {(turn.output_tokens ?? 0).toLocaleString()} out · {money(turn.cost_usd ?? 0)}</>}</p>
            {turn.text && <p className="mt-1 whitespace-pre-wrap break-words text-gray-800 dark:text-gray-200">{turn.text}</p>}
            {turn.collapsed && <details className="mt-1 text-gray-500 dark:text-gray-400"><summary>Tool or reasoning content omitted</summary><p>Not shown in this read-only view.</p></details>}
          </li>)}
        </ol>}
        {turns?.has_more && <Button type="button" variant="outline" className="mt-4" disabled={loadingTurns} onClick={() => void loadMoreTurns()}>
          {loadingTurns ? 'Loading…' : 'Load more turns'}</Button>}
      </Panel>}
    </div>
  </main>;
}
