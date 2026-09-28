/**
 * Tests for the ActivityCard component.
 *
 * Issue #3770: Responsive card layout for narrow viewports.
 * Validates: card rendering with primary info, expand/collapse of secondary
 * details, click interactions, transcript link, and accessibility attributes.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ActivityCard, type ActivityCardProps } from '@/components/activity/ActivityCard';
import type { InvocationItem } from '@/types/activity';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeItem(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'inv-test-001',
    user_id: 'user-001',
    persona: 'developer',
    channel: 'github',
    status: 'complete',
    topic: 'Implement responsive layout',
    summary: 'Added card-based view for narrow viewports',
    source_url: 'https://github.com/aws-e/adp/issues/3770',
    repo: 'aws-e/adp',
    issue_number: 3770,
    invoked_at: '2026-07-12T10:00:00Z',
    completed_at: '2026-07-12T10:30:00Z',
    status_updated_at: '2026-07-12T10:30:00Z',
    run_id: 'run-001',
    trigger_kind: 'human',
    triggered_by_invocation_id: null,
    triggered_by_topic: null,
    root_human_id: 'user-001',
    is_human_rooted: true,
    correlation_id: 'chain-001',
    total_cost_usd: 0.0523,
    total_tokens: 15000,
    call_count: 3,
    error_message: null,
    run_log_url: null,
    transcript_key: 's3://transcripts/inv-test-001.jsonl',
    ...overrides,
  };
}

function renderCard(props: Partial<ActivityCardProps> = {}) {
  const defaultProps: ActivityCardProps = {
    item: makeItem(),
    onDetailClick: vi.fn(),
    onTranscriptClick: vi.fn(),
    ...props,
  };
  return { ...render(<ActivityCard {...defaultProps} />), props: defaultProps };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ActivityCard', () => {
  it('renders primary info: topic, status, time, cost', () => {
    renderCard();

    // Topic as heading
    expect(screen.getByText('Implement responsive layout')).toBeInTheDocument();
    // Status badge
    expect(screen.getByText('Complete')).toBeInTheDocument();
    // Cost (formatted)
    expect(screen.getByText('$0.05')).toBeInTheDocument();
  });

  it('renders source link when available', () => {
    renderCard();

    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', 'https://github.com/aws-e/adp/issues/3770');
    expect(link).toHaveTextContent('aws-e/adp#3770');
  });

  it('does not render source link when source_url is null', () => {
    renderCard({ item: makeItem({ source_url: null, repo: null, issue_number: null }) });

    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('shows "untitled" in italic when topic is null', () => {
    renderCard({ item: makeItem({ topic: null }) });

    expect(screen.getByText('untitled')).toBeInTheDocument();
  });

  it('shows "More" button initially (collapsed)', () => {
    renderCard();

    const toggle = screen.getByTestId('activity-card-toggle-inv-test-001');
    expect(toggle).toHaveTextContent('More');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('expands secondary details when "More" is clicked', async () => {
    const user = userEvent.setup();
    renderCard();

    const toggle = screen.getByTestId('activity-card-toggle-inv-test-001');
    await user.click(toggle);

    // Now expanded
    expect(toggle).toHaveTextContent('Less');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    // Secondary details visible
    const details = screen.getByTestId('activity-card-details-inv-test-001');
    expect(details).toBeInTheDocument();
    expect(details).toHaveTextContent('Started by you');
    expect(details).toHaveTextContent('github');
    expect(details).toHaveTextContent('developer');
    expect(details).toHaveTextContent('Added card-based view for narrow viewports');
    expect(details).toHaveTextContent('View transcript');
  });

  it('collapses details when "Less" is clicked', async () => {
    const user = userEvent.setup();
    renderCard();

    const toggle = screen.getByTestId('activity-card-toggle-inv-test-001');
    // Expand
    await user.click(toggle);
    expect(screen.getByTestId('activity-card-details-inv-test-001')).toBeInTheDocument();

    // Collapse
    await user.click(toggle);
    expect(screen.queryByTestId('activity-card-details-inv-test-001')).not.toBeInTheDocument();
    expect(toggle).toHaveTextContent('More');
  });

  it('calls onDetailClick when card is clicked', async () => {
    const user = userEvent.setup();
    const onDetailClick = vi.fn();
    const item = makeItem();
    renderCard({ item, onDetailClick });

    const card = screen.getByTestId('activity-card-inv-test-001');
    await user.click(card);

    expect(onDetailClick).toHaveBeenCalledWith(item);
  });

  it('calls onTranscriptClick when transcript link is clicked', async () => {
    const user = userEvent.setup();
    const onTranscriptClick = vi.fn();
    renderCard({ onTranscriptClick });

    // Expand to reveal transcript link
    const toggle = screen.getByTestId('activity-card-toggle-inv-test-001');
    await user.click(toggle);

    const transcriptLink = screen.getByText('View transcript');
    await user.click(transcriptLink);

    expect(onTranscriptClick).toHaveBeenCalledWith('inv-test-001');
  });

  it('does not show transcript link when transcript_key is null', async () => {
    const user = userEvent.setup();
    renderCard({ item: makeItem({ transcript_key: null }) });

    const toggle = screen.getByTestId('activity-card-toggle-inv-test-001');
    await user.click(toggle);

    expect(screen.queryByText('View transcript')).not.toBeInTheDocument();
  });

  it('renders "pending" cost for in-progress runs with zero cost', () => {
    renderCard({ item: makeItem({ status: 'in_progress', total_cost_usd: 0 }) });

    expect(screen.getByText('pending')).toBeInTheDocument();
  });

  it('renders dash for null cost', () => {
    renderCard({ item: makeItem({ total_cost_usd: null }) });

    // The dash character
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('renders small cost with 4 decimal places', () => {
    renderCard({ item: makeItem({ total_cost_usd: 0.0012 }) });

    expect(screen.getByText('$0.0012')).toBeInTheDocument();
  });

  it('has proper aria-label for accessibility', () => {
    renderCard();

    const card = screen.getByTestId('activity-card-inv-test-001');
    expect(card).toHaveAttribute('aria-label');
    expect(card.getAttribute('aria-label')).toContain('Implement responsive layout');
    expect(card.getAttribute('aria-label')).toContain('Complete');
  });

  it('shows agent trigger info in expanded details', async () => {
    const user = userEvent.setup();
    renderCard({ item: makeItem({ trigger_kind: 'agent' }) });

    const toggle = screen.getByTestId('activity-card-toggle-inv-test-001');
    await user.click(toggle);

    expect(screen.getByText('Agent-triggered')).toBeInTheDocument();
  });
});
