import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import MyConversations, { dayLabel, relative } from '@/pages/MyConversations';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));

const page1 = { conversations: [
  { harness: 'codex_cli', conversation_id: 'thread-9', untracked: false, origin: 'dev_box', title: 'rank repos by commits',
    started_at: '2026-10-26T10:02:00Z', last_activity_at: '2026-10-26T10:10:00Z', turns: 2, models: ['openai.gpt-6-astra'], cost_usd: 0.02 },
  { harness: 'claude_code', conversation_id: 'sess-1', untracked: false, origin: 'direct', title: 'fix the login bug',
    started_at: '2026-10-26T10:00:00Z', last_activity_at: '2026-10-26T10:05:00Z', turns: 3, models: ['us.anthropic.claude-fable-5-1'], cost_usd: 0.03 },
  { harness: 'kiro_crew', conversation_id: 'untracked:2026-10-26', untracked: true, origin: 'dev_box', title: 'Untracked turns',
    started_at: '2026-10-26T10:03:00Z', last_activity_at: '2026-10-26T10:03:00Z', turns: 1, models: ['global.anthropic.claude-sonnet-5'], cost_usd: 0.01 },
  { harness: 'claude_code', conversation_id: 'owner-session-1', untracked: false, origin: 'dev_box', title: 'fix the login bug on the box',
    started_at: '2026-10-26T09:50:00Z', last_activity_at: '2026-10-26T10:01:00Z', turns: 3, requests: 1, models: ['us.anthropic.claude-fable-5-1'], cost_usd: 0.01,
    source: 'backup', partial: false, unreadable: false },
], next_before: '2026-10-26T10:01:00+00:00' };
const backupTurns = (offset: number) => offset === 0
  ? { turns: [
      { role: 'user', time: '2026-10-26T09:50:00Z', text: 'fix the login bug on the box', collapsed: false },
      { role: 'assistant', time: '2026-10-26T09:51:00Z', text: 'Looking on the box.', collapsed: true },
    ], content_available: true, content_reason: 'ok', request_count: 0, latest_request_id: null, source: 'backup', has_more: true }
  : { turns: [{ role: 'assistant', time: '2026-10-26T10:01:00Z', text: 'Fixed it on the box.', collapsed: false }],
      content_available: true, content_reason: 'ok', request_count: 0, latest_request_id: null, source: 'backup', has_more: false };
const page2 = { conversations: [
  { harness: 'web_chat', conversation_id: 'chat-7', untracked: false, origin: 'direct', title: 'older web chat',
    started_at: '2026-10-25T09:00:00Z', last_activity_at: '2026-10-25T09:30:00Z', turns: 4, models: ['global.anthropic.claude-sonnet-5'], cost_usd: 0.05 },
], next_before: null };

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset().mockImplementation(async (endpoint: string) => {
    if (endpoint.startsWith('/v1/me/conversations?') && endpoint.includes('before=')) return page2;
    if (endpoint.startsWith('/v1/me/conversations?') && endpoint.includes('harness=claude_code')) return { conversations: [page1.conversations[1]], next_before: null };
    if (endpoint.startsWith('/v1/me/conversations?')) return page1;
    if (endpoint === '/v1/me/conversations/claude_code/sess-1/turns') return { turns: [
      { role: 'user', time: null, text: 'fix the login bug', collapsed: false },
      { role: 'assistant', time: '2026-10-26T10:05:01Z', text: 'Fixed it.', collapsed: true },
    ], content_available: true, request_count: 3, latest_request_id: 'req-3' };
    if (endpoint === '/v1/me/conversations/claude_code/owner-session-1/turns?source=backup') return backupTurns(0);
    if (endpoint === '/v1/me/conversations/claude_code/owner-session-1/turns?source=backup&offset=2') return backupTurns(2);
    if (endpoint === '/v1/me/conversations/codex_cli/thread-9/turns') return { turns: [
      { role: 'request', time: '2026-10-26T10:02:00Z', text: 'rank repos by commits', collapsed: false, model: 'openai.gpt-6-astra', input_tokens: 12, output_tokens: 300, cost_usd: 0.0123 },
      { role: 'request', time: '2026-10-26T10:10:00Z', text: '', collapsed: false, model: 'openai.gpt-6-astra', input_tokens: 2, output_tokens: 40, cost_usd: 0.0077 },
    ], content_available: false, content_reason: 'not_retained', request_count: 2, latest_request_id: 'req-5' };
    throw new Error(`unexpected ${endpoint}`);
  });
});

describe('My conversations (#9027)', () => {
  it('lists conversations newest first with harness and origin badges, then opens the turns', async () => {
    render(<MyConversations />);
    const list = await screen.findByRole('list', { name: 'My conversations' });
    const items = within(list).getAllByRole('listitem');
    expect(items.map(item => within(item).getByRole('button').textContent)).toEqual(['rank repos by commits', 'fix the login bug', 'Untracked turns', 'fix the login bug on the box']);
    expect(items[0]).toHaveTextContent('Codex'); expect(items[0]).toHaveTextContent('Dev Box'); expect(items[0]).toHaveTextContent('openai.gpt-6-astra'); expect(items[0]).toHaveTextContent('2 requests');
    expect(within(list).getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual([dayLabel('2026-10-26T10:10:00Z')]);
    expect(within(items[0]).getByRole('button').getAttribute('title')).toBe('rank repos by commits');
    expect(items[1]).toHaveTextContent('Claude Code'); expect(items[1]).toHaveTextContent('Direct');
    expect(items[2]).toHaveTextContent('Kiro Crew'); expect(items[2]).toHaveTextContent('untracked');
    fireEvent.click(within(items[1]).getByRole('button', { name: 'fix the login bug' }));
    const detail = await screen.findByRole('region', { name: 'Conversation detail' });
    expect(await within(detail).findByText('Fixed it.')).toBeInTheDocument();
    expect(within(within(detail).getByRole('list', { name: 'Conversation turns' })).getByText('fix the login bug')).toBeInTheDocument();
    expect(within(detail).getByText('Tool or reasoning content omitted').closest('details')).not.toHaveAttribute('open');
    expect(within(detail).queryByRole('note')).not.toBeInTheDocument();
    expect(vi.mocked(apiClient.get).mock.calls.some(([endpoint]) => String(endpoint).includes('ssh-access') || String(endpoint).includes('/admin/'))).toBe(false);
  });

  it('says so when only request metadata exists, filters by harness, and loads older pages', async () => {
    render(<MyConversations />);
    fireEvent.click(await screen.findByRole('button', { name: 'rank repos by commits' }));
    const detail = await screen.findByRole('region', { name: 'Conversation detail' });
    expect(await within(detail).findByRole('note')).toHaveTextContent('not retained by the scrubber');
    const timeline = within(detail).getByRole('list', { name: 'Conversation turns' });
    expect(within(timeline).getAllByRole('listitem')).toHaveLength(2);
    expect(timeline).toHaveTextContent('12 in / 300 out · $0.012');
    expect(timeline).toHaveTextContent('rank repos by commits');
    fireEvent.click(screen.getByRole('button', { name: 'Load older conversations' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'older web chat' })).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Load older conversations' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: 'Harness' }), { target: { value: 'claude_code' } });
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'My conversations' })).getAllByRole('listitem')).toHaveLength(1));
    expect(screen.queryByRole('region', { name: 'Conversation detail' })).not.toBeInTheDocument();
    const encoded = vi.mocked(apiClient.get).mock.calls.map(([endpoint]) => String(endpoint)).find(endpoint => endpoint.includes('before='));
    expect(encoded).toContain(encodeURIComponent('2026-10-26T10:01:00+00:00'));
  });

  it('opens a Dev Box backup conversation from the same list, counts turns and loads more of them (#9069)', async () => {
    render(<MyConversations />);
    const list = await screen.findByRole('list', { name: 'My conversations' });
    const item = within(list).getAllByRole('listitem')[3];
    expect(item).toHaveTextContent('3 turns'); expect(item).toHaveTextContent('Claude Code'); expect(item).toHaveTextContent('Dev Box');
    expect(within(list).getAllByRole('listitem')[0]).toHaveTextContent('2 requests');
    fireEvent.click(within(item).getByRole('button', { name: 'fix the login bug on the box' }));
    const detail = await screen.findByRole('region', { name: 'Conversation detail' });
    expect(await within(detail).findByText('Looking on the box.')).toBeInTheDocument();
    expect(within(detail).queryByRole('note')).not.toBeInTheDocument();
    fireEvent.click(within(detail).getByRole('button', { name: 'Load more turns' }));
    expect(await within(detail).findByText('Fixed it on the box.')).toBeInTheDocument();
    expect(within(within(detail).getByRole('list', { name: 'Conversation turns' })).getAllByRole('listitem')).toHaveLength(3);
    expect(within(detail).queryByRole('button', { name: 'Load more turns' })).not.toBeInTheDocument();
    const calls = vi.mocked(apiClient.get).mock.calls.map(([endpoint]) => String(endpoint));
    expect(calls).toContain('/v1/me/conversations/claude_code/owner-session-1/turns?source=backup');
    expect(calls).toContain('/v1/me/conversations/claude_code/owner-session-1/turns?source=backup&offset=2');
  });

  it('shows an alert with retry when the list fails, and an empty state when there is nothing', async () => {
    vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('boom'));
    render(<MyConversations />);
    expect(await screen.findByText('Could not load your conversations')).toBeInTheDocument();
    vi.mocked(apiClient.get).mockResolvedValueOnce({ conversations: [], next_before: null });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
  });
});


describe('formatting helpers', () => {
  it('shows relative times with the exact time kept for the tooltip, and groups by day', () => {
    const now = Date.parse('2026-10-27T10:00:00Z');
    expect(relative('2026-10-27T09:59:40Z', now)).toBe('just now');
    expect(relative('2026-10-27T09:48:00Z', now)).toBe('12 min ago');
    expect(relative('2026-10-27T07:00:00Z', now)).toBe('3 h ago');
    expect(relative('2026-10-25T10:00:00Z', now)).toBe('2 d ago');
    expect(relative(null, now)).toBe('Unknown');
    const today = new Date('2026-10-27T10:00:00Z');
    expect(dayLabel('2026-10-27T01:00:00Z', today)).toBe('Today');
    expect(dayLabel('2026-10-26T23:00:00Z', today)).toBe('Yesterday');
    expect(dayLabel(null, today)).toBe('Unknown day');
  });
});
