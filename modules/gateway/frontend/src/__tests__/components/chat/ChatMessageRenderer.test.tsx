/**
 * Unit tests for ChatMessageRenderer component.
 *
 * Issue #97 Phase 1: verifies markdown rendering, code blocks with
 * syntax highlighting and copy button, and error/system message display.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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
    Object.assign(navigator, {
      clipboard: {
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

  it('renders GFM tables', () => {
    const md = '| Header |\n|--------|\n| Cell |';
    const msg = makeMessage({ content: md });
    render(<ChatMessageRenderer message={msg} />);

    const table = screen.getByTestId('message-content').querySelector('table');
    expect(table).toBeTruthy();
  });
});
