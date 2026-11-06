/**
 * Unit tests for ChatMessageRenderer component.
 *
 * Issue #97 Phase 1: verifies markdown rendering, code blocks with
 * syntax highlighting and copy button, and error/system message display.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatMessageRenderer } from '@/components/chat/ChatMessageRenderer';
import type { ChatMessage } from '@/types/chat';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'msg-1',
    role: 'assistant',
    content: 'Hello **world**',
    status: 'complete',
    timestamp: Date.now(),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ChatMessageRenderer', () => {
  beforeEach(() => {
    // Mock clipboard API
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: vi.fn(() => Promise.resolve()),
      },
    });
  });

  it('renders user messages with correct alignment', () => {
    const msg = makeMessage({ role: 'user', content: 'Hi there' });
    const { container } = render(<ChatMessageRenderer message={msg} />);

    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain('justify-end');
  });

  it('renders assistant messages with correct alignment', () => {
    const msg = makeMessage({ role: 'assistant', content: 'Hello' });
    const { container } = render(<ChatMessageRenderer message={msg} />);

    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain('justify-start');
  });

  it('renders system messages centered', () => {
    const msg = makeMessage({ role: 'system', content: 'Task received' });
    render(<ChatMessageRenderer message={msg} />);

    const el = screen.getByTestId('system-message');
    expect(el).toBeTruthy();
    expect(el.textContent).toContain('Task received');
  });

  it('renders markdown bold text', () => {
    const msg = makeMessage({ content: 'This is **bold** text' });
    render(<ChatMessageRenderer message={msg} />);

    const content = screen.getByTestId('message-content');
    const strong = content.querySelector('strong');
    expect(strong).toBeTruthy();
    expect(strong?.textContent).toBe('bold');
  });

  it('renders markdown links with target=_blank', () => {
    const msg = makeMessage({ content: 'Visit [Google](https://google.com)' });
    render(<ChatMessageRenderer message={msg} />);

    const link = screen.getByTestId('message-content').querySelector('a');
    expect(link).toBeTruthy();
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.getAttribute('rel')).toContain('noopener');
  });

  it('renders code blocks with copy button', async () => {
    const msg = makeMessage({ content: '```js\nconsole.log("hello");\n```' });
    render(<ChatMessageRenderer message={msg} />);

    const copyBtn = screen.getByTestId('copy-code-button');
    expect(copyBtn).toBeTruthy();

    fireEvent.click(copyBtn);

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining('console.log')
      );
    });
  });

  it('renders inline code without copy button', () => {
    const msg = makeMessage({ content: 'Use `npm install` to install' });
    render(<ChatMessageRenderer message={msg} />);

    const content = screen.getByTestId('message-content');
    const inlineCode = content.querySelector('code');
    expect(inlineCode).toBeTruthy();

    // No copy button for inline code
    expect(screen.queryByTestId('copy-code-button')).toBeNull();
  });

  it('renders error messages with error reason', () => {
    const msg = makeMessage({
      status: 'error',
      content: 'Something went wrong',
      errorReason: 'Model overloaded',
    });
    render(<ChatMessageRenderer message={msg} />);

    const reason = screen.getByTestId('error-reason');
    expect(reason.textContent).toContain('Model overloaded');
  });

  it('shows typing indicator for streaming messages with no content', () => {
    const msg = makeMessage({
      status: 'streaming',
      content: '',
    });
    render(<ChatMessageRenderer message={msg} />);

    const indicator = screen.getByRole('status');
    expect(indicator).toBeTruthy();
  });

  it('shows tool use indicator during streaming', () => {
    const msg = makeMessage({
      status: 'streaming',
      content: 'Working on it...',
      toolUse: { tool_name: 'WebSearch' },
    });
    render(<ChatMessageRenderer message={msg} />);

    // Should show tool indicator
    expect(screen.getByText(/Searching the web/)).toBeTruthy();
  });

  it('displays timestamp for messages', () => {
    const now = new Date();
    const msg = makeMessage({ timestamp: now.getTime() });
    const { container } = render(<ChatMessageRenderer message={msg} />);

    const timeText = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(container.textContent).toContain(timeText);
  });

  it('renders reported window, freshness, coverage and authorized source links', () => {
    render(<ChatMessageRenderer message={makeMessage({ content: 'Two runs completed.', evidence: {
      status: 'ok', from: '2026-10-01T00:00:00Z', to: '2026-10-02T00:00:00Z',
      timezone: 'America/New_York', observed_at: '2026-10-02T00:05:00Z',
      coverage: [{ source: 'activity.adp', status: 'available' }],
      sources: [
        { label: 'Run record', url: '/activity?id=run-1' },
        { label: 'Issue', url: 'https://github.com/example/project/issues/12' },
      ],
    } })} />);
    const evidence = screen.getByRole('region', { name: 'Answer evidence' });
    expect(evidence).toHaveTextContent('Sources reported available');
    expect(evidence).toHaveTextContent('2026-10-01T00:00:00Z (inclusive) to 2026-10-02T00:00:00Z (exclusive); America/New_York');
    expect(evidence).toHaveTextContent('Freshness: Observed at 2026-10-02T00:05:00Z');
    expect(evidence).toHaveTextContent('activity.adp: available');
    expect(screen.getByRole('link', { name: 'Run record' })).toHaveAttribute('href', '/activity?id=run-1');
    expect(screen.getByRole('link', { name: 'Issue' })).toHaveAttribute('href', 'https://github.com/example/project/issues/12');
    expect(screen.getByRole('link', { name: 'Issue' })).toHaveAttribute('rel', 'noopener noreferrer');
  });
  it.each([
    ['empty', 'empty', 'No records reported in the covered window'],
    ['denied', 'denied', 'Permission denied: records were not available to this request'],
    ['unavailable', 'unavailable', 'Source unavailable: missing records cannot be treated as empty'],
    ['partial', 'partial', 'Partial results: some sources or pages are missing'],
  ] as const)('distinguishes %s coverage without presenting a missing source as empty', (status, sourceStatus, label) => {
    render(<ChatMessageRenderer message={makeMessage({
      status: status === 'denied' ? 'error' : 'complete',
      content: status === 'empty' ? '' : 'Available observations',
      evidence: { status, coverage: [{ source: 'activity.external', status: sourceStatus, reason: 'fixture' }] },
    })} />);
    const evidence = screen.getByRole('region', { name: 'Answer evidence' });
    expect(evidence).toHaveTextContent(label);
    expect(evidence).toHaveTextContent(`activity.external: ${sourceStatus} (fixture)`);
    expect(evidence).toHaveTextContent('Window or timezone not reported');
    expect(evidence).toHaveTextContent('Freshness: Not reported');
    expect(evidence).toHaveTextContent('Source links not reported');
    if (status !== 'empty') expect(evidence).not.toHaveTextContent('No records reported in the covered window');
  });

  it('treats an empty report without source coverage as unverified', () => {
    render(<ChatMessageRenderer message={makeMessage({ content: '', evidence: { status: 'empty' } })} />);
    expect(screen.getByRole('region', { name: 'Answer evidence' })).toHaveTextContent('Empty reported; source coverage unverified');
  });

  it('treats a continuation and an unavailable covered source as partial, even if a payload says ok or empty', () => {
    const page = render(<ChatMessageRenderer message={makeMessage({ evidence: {
      status: 'ok', has_more: true, coverage: [{ source: 'activity.adp', status: 'available' }],
    } })} />);
    expect(screen.getByRole('region', { name: 'Answer evidence' })).toHaveTextContent('Partial results: some sources or pages are missing');
    expect(screen.getByRole('region', { name: 'Answer evidence' })).toHaveTextContent('More pages remain; this window is incomplete.');
    page.rerender(<ChatMessageRenderer message={makeMessage({ evidence: {
      status: 'empty', coverage: [{ source: 'activity.external', status: 'unavailable', reason: 'provider_outage' }],
    } })} />);
    const evidence = screen.getByRole('region', { name: 'Answer evidence' });
    expect(evidence).toHaveTextContent('Partial results: some sources or pages are missing');
    expect(evidence).toHaveTextContent('activity.external: unavailable (provider_outage)');
    expect(evidence).not.toHaveTextContent('No records reported in the covered window');
  });

  it('marks an answer without structured evidence as unverified rather than supplying a guessed window', () => {
    render(<ChatMessageRenderer message={makeMessage({ content: 'Answer without source metadata' })} />);
    const evidence = screen.getByRole('region', { name: 'Answer evidence' });
    expect(evidence).toHaveTextContent('Source window, freshness and coverage not reported for this answer.');
    expect(evidence).not.toHaveTextContent('Sources reported available');
  });

  it('never renders unsafe source URLs as links while retaining safe references', () => {
    render(<ChatMessageRenderer message={makeMessage({ evidence: {
      status: 'partial', sources: [
        { label: 'Script', url: 'javascript:alert(1)' },
        { label: 'Insecure', url: 'http://example.test/record' },
        { label: 'Protocol-relative', url: '//example.test/record' },
        { label: 'Credentials', url: 'https://user:pass@example.test/record' },
        { label: 'Backslash', url: '/\\example.test/record' },
        { label: 'Run record', url: '/activity?id=run-1' },
      ],
    } })} />);
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'Run record' })).toHaveAttribute('href', '/activity?id=run-1');
    for (const name of ['Script', 'Insecure', 'Protocol-relative', 'Credentials', 'Backslash']) {
      expect(screen.queryByRole('link', { name })).not.toBeInTheDocument();
    }
  });
  it('keeps long markdown answers and their evidence reachable by keyboard', async () => {
    const longContent = `${'Recorded work details. '.repeat(900)}\n\nEnd of answer. [Written source](https://github.com/example/project/issues/12)`;
    render(<ChatMessageRenderer message={makeMessage({ content: longContent, evidence: {
      status: 'partial', coverage: [{ source: 'activity.external', status: 'partial', reason: 'more_pages' }],
      sources: [{ label: 'Activity record', url: '/activity?id=run-1' }],
    } })} />);
    const content = screen.getByTestId('message-content');
    expect(content.textContent?.length).toBeGreaterThan(18_000);
    expect(content).toHaveTextContent('End of answer.');
    expect(screen.getByRole('region', { name: 'Answer evidence' })).toHaveTextContent('activity.external: partial (more_pages)');
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole('link', { name: 'Written source' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('link', { name: 'Activity record' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole('link', { name: 'Written source' })).toHaveFocus();
  });
  it('renders GFM tables', () => {
    const md = '| Header |\n|--------|\n| Cell |';
    const msg = makeMessage({ content: md });
    render(<ChatMessageRenderer message={msg} />);

    const table = screen.getByTestId('message-content').querySelector('table');
    expect(table).toBeTruthy();
  });
});
