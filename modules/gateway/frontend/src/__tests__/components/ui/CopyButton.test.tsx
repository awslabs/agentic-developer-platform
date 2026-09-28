/**
 * CopyButton tests — Issue #4146.
 *
 * navigator.clipboard is not globally mocked by src/test/setup.ts, so it is
 * stubbed per-test here.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CopyButton } from '@/components/ui/CopyButton';

function stubClipboard(writeText = vi.fn().mockResolvedValue(undefined)) {
  Object.defineProperty(navigator, 'clipboard', {
    writable: true,
    configurable: true,
    value: { writeText },
  });
  return writeText;
}

afterEach(() => vi.restoreAllMocks());

describe('CopyButton', () => {
  it('writes the value to the clipboard', async () => {
    const writeText = stubClipboard();
    render(<CopyButton value="hello world" />);

    fireEvent.click(screen.getByRole('button'));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hello world'));
  });

  it('shows a confirmation state after copying', async () => {
    stubClipboard();
    render(<CopyButton value="x" />);

    expect(screen.getByRole('button')).toHaveTextContent('Copy');
    fireEvent.click(screen.getByRole('button'));

    await waitFor(() => expect(screen.getByRole('button')).toHaveTextContent('Copied'));
  });

  it('honours custom labels', async () => {
    stubClipboard();
    render(<CopyButton value="x" label="Copy token" copiedLabel="Token copied" />);

    expect(screen.getByRole('button', { name: 'Copy token' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button'));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Token copied' })).toBeInTheDocument()
    );
  });

  it('falls back to execCommand when the clipboard API rejects', async () => {
    // Insecure contexts (plain http) have no working navigator.clipboard.
    stubClipboard(vi.fn().mockRejectedValue(new Error('not allowed')));
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, 'execCommand', {
      writable: true,
      configurable: true,
      value: execCommand,
    });

    render(<CopyButton value="fallback text" />);
    fireEvent.click(screen.getByRole('button'));

    await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'));
    expect(screen.getByRole('button')).toHaveTextContent('Copied');
  });
});
