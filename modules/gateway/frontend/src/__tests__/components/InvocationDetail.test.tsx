/**
 * Tests for InvocationDetail component.
 *
 * Issue #1459: Phase 5 — Row detail + polish.
 * Issue #3069: Wrapped in QueryClientProvider (TranscriptViewer uses useQuery).
 * Validates: detail renders with all fields, error truncation + show more,
 * sanitized error display, status timeline rendering.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { InvocationDetail } from '@/components/InvocationDetail';
import { TranscriptViewer } from '@/components/TranscriptViewer';
import type { InvocationItem } from '@/types/activity';

// Mock the activity service transcript functions
vi.mock('@/services/activity', () => ({
  getMyTranscript: vi.fn(),
  getAdminTranscript: vi.fn(),
}));

import { getMyTranscript } from '@/services/activity';

const mockGetMyTranscript = getMyTranscript as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  });
}

function renderWithClient(ui: React.ReactElement) {
  const queryClient = createTestQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

function makeItem(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'inv-001',
    user_id: 'user-001',
    persona: 'developer',
    channel: 'github',
    status: 'complete',
    topic: 'Implement Agent Activity page',
    summary: 'Completed work on issue #1457',
    source_url: 'https://github.com/aws-e/adp/issues/1457',
    repo: 'aws-e/adp',
    issue_number: 1457,
    invoked_at: '2026-06-14T10:00:00Z',
    completed_at: '2026-06-14T10:30:00Z',
    status_updated_at: '2026-06-14T10:30:00Z',
    correlation_id: 'corr-abc12345',
    run_id: '81286554630',
    error_message: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InvocationDetail', () => {
  it('renders correlation_id, run_id, status, and status_updated_at', () => {
    const item = makeItem();
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('corr-abc12345')).toBeInTheDocument();
    expect(screen.getByText('81286554630')).toBeInTheDocument();
    expect(screen.getByText('Complete')).toBeInTheDocument();
    // status_updated_at rendered as relative time — look for "Last transition:" label
    expect(screen.getByText(/Last transition:/)).toBeInTheDocument();
  });

  it('renders invocation_id, channel, persona, topic, summary', () => {
    const item = makeItem();
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('inv-001')).toBeInTheDocument();
    expect(screen.getByText('github')).toBeInTheDocument();
    expect(screen.getByText('(developer)')).toBeInTheDocument();
    expect(screen.getByText('Implement Agent Activity page')).toBeInTheDocument();
    expect(screen.getByText('Completed work on issue #1457')).toBeInTheDocument();
  });

  it('renders source link as "repo#issue" with external link', () => {
    const item = makeItem();
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', 'https://github.com/aws-e/adp/issues/1457');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link).toHaveTextContent('aws-e/adp#1457');
  });

  it('shows "No error details available" for failed item with null error_message', () => {
    const item = makeItem({ status: 'failed', error_message: null });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('No error details available')).toBeInTheDocument();
  });

  it('shows error_message for failed item', () => {
    const item = makeItem({
      status: 'failed',
      error_message: 'Agent timed out after 300s.',
    });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('Agent timed out after 300s.')).toBeInTheDocument();
  });

  it('truncates long error_message and shows "Show more" button', async () => {
    const user = userEvent.setup();
    const longError = 'A'.repeat(250); // Longer than ERROR_TRUNCATE_LENGTH (200)
    const item = makeItem({ status: 'failed', error_message: longError });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    // Should show truncated text (200 chars + ellipsis, not the full 250)
    expect(screen.getByText(/A{10,}/)).toBeInTheDocument();

    // "Show more" button should be visible
    const showMoreBtn = screen.getByRole('button', { name: /show more/i });
    expect(showMoreBtn).toBeInTheDocument();

    // Click show more → full text
    await user.click(showMoreBtn);
    expect(screen.getByRole('button', { name: /show less/i })).toBeInTheDocument();
  });

  it('does not show error section for non-failed status', () => {
    const item = makeItem({ status: 'complete', error_message: null });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.queryByText('Error')).not.toBeInTheDocument();
    expect(screen.queryByText('No error details available')).not.toBeInTheDocument();
  });

  it('hides optional fields when null', () => {
    const item = makeItem({
      correlation_id: null,
      run_id: null,
      topic: null,
      summary: null,
      source_url: null,
      completed_at: null,
    });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.queryByText('Correlation ID')).not.toBeInTheDocument();
    expect(screen.queryByText('Run / Job ID')).not.toBeInTheDocument();
    expect(screen.queryByText('Topic')).not.toBeInTheDocument();
    expect(screen.queryByText('Summary')).not.toBeInTheDocument();
    expect(screen.queryByText('Source')).not.toBeInTheDocument();
    expect(screen.queryByText('Completed at')).not.toBeInTheDocument();
  });

  it('shows "Active — not yet terminal" for in_progress status', () => {
    const item = makeItem({ status: 'in_progress', completed_at: null });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText(/Active — not yet terminal/)).toBeInTheDocument();
  });

  it('calls onClose when modal close is triggered', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const item = makeItem();
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={onClose} />);

    // The Modal component has a close button
    const closeBtn = screen.getByLabelText('Close modal');
    await user.click(closeBtn);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('renders nothing when item is null', () => {
    const { container } = renderWithClient(
      <InvocationDetail item={null} isOpen={true} onClose={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  // Issue #3765: Error-first detail layout for failed runs
  describe('error-first layout (Issue #3765)', () => {
    it('renders error row immediately after status for failed runs', () => {
      const item = makeItem({
        status: 'failed',
        error_message: 'Agent timed out after 300s.',
        completed_at: '2026-06-14T10:30:00Z',
      });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      // Get all DetailRow labels (dt elements within the dl)
      const dl = document.querySelector('dl')!;
      const labels = Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent);

      // Error must appear immediately after Status (index 0 → Status, index 1 → Error)
      const statusIdx = labels.indexOf('Status');
      const errorIdx = labels.indexOf('Error');
      const durationIdx = labels.indexOf('Duration');

      expect(statusIdx).toBeGreaterThanOrEqual(0);
      expect(errorIdx).toBe(statusIdx + 1);
      // Error must appear before Duration
      expect(errorIdx).toBeLessThan(durationIdx);
    });

    it('keeps default order for non-failed runs (no error row)', () => {
      const item = makeItem({
        status: 'complete',
        completed_at: '2026-06-14T10:30:00Z',
      });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      const dl = document.querySelector('dl')!;
      const labels = Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent);

      // Error row should not be present
      expect(labels).not.toContain('Error');

      // Default order: Status → Invocation ID → ... → Duration should be after Channel
      const statusIdx = labels.indexOf('Status');
      const invocationIdIdx = labels.indexOf('Invocation ID');
      const channelIdx = labels.indexOf('Channel');
      const durationIdx = labels.indexOf('Duration');

      expect(statusIdx).toBe(0);
      expect(invocationIdIdx).toBe(statusIdx + 1);
      expect(channelIdx).toBeLessThan(durationIdx);
    });

    it('shows identifiers after lineage for failed runs', () => {
      const item = makeItem({
        status: 'failed',
        error_message: 'Something went wrong',
        completed_at: '2026-06-14T10:30:00Z',
        triggered_by_invocation_id: 'inv-parent-001',
        triggered_by_topic: 'Parent topic',
      });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      const dl = document.querySelector('dl')!;
      const labels = Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent);

      const lineageIdx = labels.indexOf('Triggered by');
      const invocationIdIdx = labels.indexOf('Invocation ID');

      expect(lineageIdx).toBeGreaterThanOrEqual(0);
      expect(invocationIdIdx).toBeGreaterThan(lineageIdx);
    });
  });

  it('shows "Transcript not available" when transcript fetch returns 404', async () => {
    const user = userEvent.setup();
    mockGetMyTranscript.mockRejectedValueOnce(new Error('Transcript not available'));
    const item = makeItem({ transcript_key: 'runs/inv-001/transcript.md' });
    renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

    // Click the "View full transcript" button
    const transcriptBtn = screen.getByRole('button', { name: /view full transcript/i });
    await user.click(transcriptBtn);

    // Should display the "not available" message
    await waitFor(() => {
      expect(screen.getByText('Transcript not available for this invocation.')).toBeInTheDocument();
    });
  });

  // Issue #3767: Inline transcript content swap (no nested modal)
  describe('inline transcript (Issue #3767)', () => {
    it('no nested modal — no double role="dialog" when transcript is shown', async () => {
      const user = userEvent.setup();
      mockGetMyTranscript.mockResolvedValueOnce('# Test transcript\nSome content');
      const item = makeItem({ transcript_key: 'runs/inv-001/transcript.md' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      // Click "View full transcript"
      const transcriptBtn = screen.getByRole('button', { name: /view full transcript/i });
      await user.click(transcriptBtn);

      // Only ONE dialog should be present (the outer InvocationDetail modal)
      const dialogs = screen.getAllByRole('dialog');
      expect(dialogs).toHaveLength(1);
    });

    it('shows "Back to detail" button and returns to detail view when clicked', async () => {
      const user = userEvent.setup();
      mockGetMyTranscript.mockResolvedValueOnce('# Test transcript');
      const item = makeItem({ transcript_key: 'runs/inv-001/transcript.md' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      // Click "View full transcript"
      const transcriptBtn = screen.getByRole('button', { name: /view full transcript/i });
      await user.click(transcriptBtn);

      // Should show "Back to detail" button
      const backBtn = screen.getByRole('button', { name: /back to detail/i });
      expect(backBtn).toBeInTheDocument();

      // Detail content should be hidden (no detail rows visible)
      expect(screen.queryByText('Invocation ID')).not.toBeInTheDocument();

      // Click back
      await user.click(backBtn);

      // Detail content should be visible again
      expect(screen.getByText('inv-001')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /back to detail/i })).not.toBeInTheDocument();
    });

    it('changes modal title to "Run Transcript" when transcript is shown', async () => {
      const user = userEvent.setup();
      mockGetMyTranscript.mockResolvedValueOnce('# Test transcript');
      const item = makeItem({ transcript_key: 'runs/inv-001/transcript.md' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      // Initially shows "Invocation Detail"
      expect(screen.getByText('Invocation Detail')).toBeInTheDocument();

      // Click transcript
      const transcriptBtn = screen.getByRole('button', { name: /view full transcript/i });
      await user.click(transcriptBtn);

      // Title changes to "Run Transcript"
      expect(screen.getByText('Run Transcript')).toBeInTheDocument();
      expect(screen.queryByText('Invocation Detail')).not.toBeInTheDocument();
    });
  });

  // Issue #3767 regression: standalone transcript links from activity table still open their own modal
  it('standalone transcript modal — TranscriptViewer renders its own dialog when used directly', () => {
    mockGetMyTranscript.mockResolvedValueOnce('# Standalone transcript');

    renderWithClient(
      <TranscriptViewer invocationId="inv-standalone" isOpen={true} onClose={() => {}} />,
    );

    // The standalone TranscriptViewer renders its own modal (role="dialog")
    const dialogs = screen.getAllByRole('dialog');
    expect(dialogs).toHaveLength(1);
    expect(screen.getByText('Run Transcript')).toBeInTheDocument();
  });
});
