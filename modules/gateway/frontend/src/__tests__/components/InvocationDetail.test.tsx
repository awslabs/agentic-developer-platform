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
    skip_reason: null,
    stop_reason: null,
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

    expect(screen.getAllByText('inv-001').length).toBeGreaterThan(0);
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

      // Summary prioritizes outcome; identifiers remain in the debugging panel.
      expect(labels[0]).toBe('Status');
      expect(labels).toContain('Duration');
      expect(screen.getByText('Debugging context')).toBeInTheDocument();
      expect(screen.getByText('Invocation ID')).toBeInTheDocument();
    });

    it('retains lineage and identifiers in the debugging panel for failed runs', () => {
      const item = makeItem({
        status: 'failed',
        error_message: 'Something went wrong',
        completed_at: '2026-06-14T10:30:00Z',
        triggered_by_invocation_id: 'inv-parent-001',
        triggered_by_topic: 'Parent topic',
      });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      const debugging = screen.getByText('Debugging context').closest('details')!;
      const labels = Array.from(debugging.querySelectorAll('dt')).map(dt => dt.textContent);
      expect(labels).toContain('Triggered by');
      expect(labels).toContain('Invocation ID');
    });
  });

  // Issue #4020: the "why didn't anything run" row
  describe('skip reason row (Issue #4020)', () => {
    it.each([
      ['no_op' as const, 'no_mention', /No agent was mentioned/],
      ['blocked' as const, 'self_re_trigger', /infinite loop/],
      ['skipped' as const, 'idempotency_merged_pr', /already exists/],
    ])('explains the reason for %s runs', (status, skipReason, expected) => {
      const item = makeItem({ status, skip_reason: skipReason, summary: null });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText('Reason')).toBeInTheDocument();
      expect(screen.getByText(expected)).toBeInTheDocument();
    });

    it('also shows the raw enum so it can be searched in CloudWatch', () => {
      // The enum is the exact term that appears in the Lambda's logs and
      // metrics, which is where an operator goes next after reading the prose.
      const item = makeItem({ status: 'no_op', skip_reason: 'label_unmapped' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText('label_unmapped')).toBeInTheDocument();
    });

    it('humanizes a reason this build does not know about', () => {
      // The Lambda deploys independently of the SPA, so an unmapped enum is
      // expected. Falling back to a blank row would recreate the original bug.
      const item = makeItem({ status: 'blocked', skip_reason: 'some_future_guard' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText('Some future guard')).toBeInTheDocument();
    });

    it('says so explicitly when a pre-existing row has no reason', () => {
      // DDB is schemaless and there was no backfill, so rows written before this
      // change carry nothing. Saying "we don't know" beats an absent row, which
      // would be indistinguishable from the old unexplained badge.
      const item = makeItem({ status: 'no_op', skip_reason: null });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText(/predates reason tracking/)).toBeInTheDocument();
    });

    it('is not styled as an error', () => {
      // A stopped loop or a deduplicated redelivery is the guard working
      // correctly. Rendering it through the red ErrorDisplay would report a
      // correct decision as a fault and send operators chasing a non-incident.
      const item = makeItem({ status: 'blocked', skip_reason: 'chain_depth_exceeded' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      const dl = document.querySelector('dl')!;
      const labels = Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent);
      expect(labels).toContain('Reason');
      expect(labels).not.toContain('Error');
    });

    it('appears immediately after status so it is the first thing read', () => {
      const item = makeItem({ status: 'no_op', skip_reason: 'no_mention' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      const dl = document.querySelector('dl')!;
      const labels = Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent);
      expect(labels.indexOf('Reason')).toBe(labels.indexOf('Status') + 1);
    });

    it('is absent for statuses where something actually ran', () => {
      // Regression: a reason beside "Complete" would describe why nothing ran on
      // a row where something did.
      for (const status of ['complete', 'in_progress', 'failed', 'rate_limited'] as const) {
        const { unmount } = renderWithClient(
          <InvocationDetail
            item={makeItem({ status, skip_reason: 'no_mention' })}
            isOpen={true}
            onClose={() => {}}
          />,
        );
        expect(screen.queryByText('Reason')).not.toBeInTheDocument();
        unmount();
      }
    });

    it('derives a duration for blocked/skipped runs instead of "Active"', () => {
      // Both are terminal — the row will never transition again. Without them in
      // the terminal set the modal claimed the run was still active forever.
      const item = makeItem({ status: 'skipped', skip_reason: 'idempotency_merged_pr' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.queryByText(/Active — not yet terminal/)).not.toBeInTheDocument();
    });
  });

  // Issue #4187: the "why did this stop early" row
  describe('stop reason row (Issue #4187)', () => {
    it('names the cap that stopped the run', () => {
      const item = makeItem({ status: 'budget_stopped', stop_reason: 'run_cap_exceeded' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText('Stopped because')).toBeInTheDocument();
      expect(screen.getByText(/per-run spend cap/i)).toBeInTheDocument();
      // The enum is the term that appears in the gateway's logs and metrics.
      expect(screen.getByText('run_cap_exceeded')).toBeInTheDocument();
    });

    it('humanizes a cap this build does not know about', () => {
      // The agent image and the SPA deploy independently, so an unmapped enum is
      // expected rather than exceptional.
      const item = makeItem({ status: 'budget_stopped', stop_reason: 'some_future_cap' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText('Some future cap')).toBeInTheDocument();
    });

    it('still explains itself when no specific cap was recorded', () => {
      const item = makeItem({ status: 'budget_stopped', stop_reason: null });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.getByText(/no specific cap was recorded/)).toBeInTheDocument();
    });

    it('is not styled as an error', () => {
      // A cap firing is the control working. Routing it through the red
      // ErrorDisplay would send an operator to debug a run that behaved
      // correctly — the budget decision is the actual next step.
      const item = makeItem({ status: 'budget_stopped', stop_reason: 'run_cap_exceeded' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      const dl = document.querySelector('dl')!;
      const labels = Array.from(dl.querySelectorAll('dt')).map((dt) => dt.textContent);
      expect(labels).toContain('Stopped because');
      expect(labels).not.toContain('Error');
    });

    it('is absent for runs no cap stopped', () => {
      // Regression: a stale stop_reason must not render beside "Complete".
      const item = makeItem({ status: 'complete', stop_reason: 'run_cap_exceeded' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.queryByText('Stopped because')).not.toBeInTheDocument();
    });

    it('is terminal, so the modal does not claim the run is still active', () => {
      // Without budget_stopped in the terminal set the modal would report a run
      // that will never transition again as running forever.
      const item = makeItem({ status: 'budget_stopped', stop_reason: 'chain_cap_exceeded' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      expect(screen.queryByText(/Active — not yet terminal/)).not.toBeInTheDocument();
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

      // The transcript retains the run metadata and controls.
      expect(screen.getByText('Invocation ID')).toBeInTheDocument();
      expect(screen.getByText('Run summary')).toBeInTheDocument();

      // Click back
      await user.click(backBtn);

      // Detail content should be visible again
      expect(screen.getAllByText('inv-001').length).toBeGreaterThan(0);
      expect(screen.queryByRole('button', { name: /back to detail/i })).not.toBeInTheDocument();
    });

    it('keeps the workspace identity when transcript is shown', async () => {
      const user = userEvent.setup();
      mockGetMyTranscript.mockResolvedValueOnce('# Test transcript');
      const item = makeItem({ transcript_key: 'runs/inv-001/transcript.md' });
      renderWithClient(<InvocationDetail item={item} isOpen={true} onClose={() => {}} />);

      // Initially shows "Invocation Detail"
      expect(screen.getByText('Run workspace')).toBeInTheDocument();

      // Click transcript
      const transcriptBtn = screen.getByRole('button', { name: /view full transcript/i });
      await user.click(transcriptBtn);

      // Title changes to "Run Transcript"
      expect(screen.getByText('Run workspace')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Transcript & run record' })).toHaveAttribute('aria-pressed', 'true');
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
