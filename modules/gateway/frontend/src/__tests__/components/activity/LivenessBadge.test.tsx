/**
 * Tests for the liveness verdict rendering (issue #4176).
 *
 * Two things matter here beyond "does it render":
 *
 * 1. `unverifiable` must be visually and textually DISTINCT from a healthy
 *    in-progress run. The bug being fixed is that a stalled run was
 *    indistinguishable from a live one, so a badge that looked the same would
 *    fix nothing.
 * 2. An unrecognised verdict must not throw. The backend can add values on its
 *    own cadence; a board that crashes on an unknown string is worse than one
 *    that quietly omits a badge.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { LivenessBadge } from '@/components/activity/LivenessBadge';
import { ActivityCard, type ActivityCardProps } from '@/components/activity/ActivityCard';
import { describeLiveness, isAttentionWorthy } from '@/utils/liveness';
import type { InvocationItem } from '@/types/activity';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeItem(overrides: Partial<InvocationItem> = {}): InvocationItem {
  return {
    invocation_id: 'inv-test-4176',
    user_id: 'user-001',
    persona: 'developer',
    channel: 'github',
    status: 'in_progress',
    topic: 'A run that stopped reporting',
    summary: null,
    source_url: null,
    repo: 'aws-e/adp',
    issue_number: 4176,
    invoked_at: '2026-08-20T10:00:00Z',
    completed_at: null,
    status_updated_at: '2026-08-20T10:00:05Z',
    run_id: 'run-4176',
    trigger_kind: 'human',
    triggered_by_invocation_id: null,
    triggered_by_topic: null,
    root_human_id: 'user-001',
    is_human_rooted: true,
    correlation_id: 'chain-4176',
    total_cost_usd: null,
    total_tokens: null,
    call_count: null,
    error_message: null,
    skip_reason: null,
    run_log_url: null,
    transcript_key: null,
    ...overrides,
  };
}

function renderCard(item: InvocationItem) {
  const props: ActivityCardProps = {
    item,
    onDetailClick: vi.fn(),
    onTranscriptClick: vi.fn(),
  };
  return render(<ActivityCard {...props} />);
}

// ---------------------------------------------------------------------------
// Badge rendering
// ---------------------------------------------------------------------------

describe('LivenessBadge', () => {
  it('renders a label for each known verdict', () => {
    const { rerender } = render(<LivenessBadge verdict="live" />);
    expect(screen.getByText('Live')).toBeInTheDocument();

    rerender(<LivenessBadge verdict="unverifiable" />);
    expect(screen.getByText('Unverifiable')).toBeInTheDocument();

    rerender(<LivenessBadge verdict="exited" />);
    expect(screen.getByText('Exited')).toBeInTheDocument();
  });

  it('renders unverifiable visually distinct from live', () => {
    const { container: unverifiable } = render(<LivenessBadge verdict="unverifiable" />);
    const unverifiableClasses = unverifiable.querySelector('span')?.className ?? '';

    const { container: live } = render(<LivenessBadge verdict="live" />);
    const liveClasses = live.querySelector('span')?.className ?? '';

    // Not merely different text — a different colour register, so the two are
    // tellable apart at a glance on a dense board.
    expect(unverifiableClasses).not.toEqual(liveClasses);
    expect(unverifiableClasses).toContain('amber');
    expect(liveClasses).not.toContain('amber');
  });

  it('explains unverifiable without claiming the run ended', () => {
    render(<LivenessBadge verdict="unverifiable" />);
    const badge = screen.getByTitle(/cannot confirm/i);
    expect(badge).toBeInTheDocument();
    // The wording must not license "this run is dead" — that reading is the
    // exact failure mode #4176 exists to prevent.
    expect(badge.getAttribute('title')).toMatch(/not a claim that it stopped/i);
  });

  it('does not throw on an unknown verdict, and renders nothing', () => {
    expect(() => render(<LivenessBadge verdict="teleported_sideways" />)).not.toThrow();
    expect(screen.queryByText(/teleported/i)).not.toBeInTheDocument();
  });

  it.each([[null], [undefined], ['']])('renders nothing for absent verdict %s', (verdict) => {
    const { container } = render(<LivenessBadge verdict={verdict as string | null | undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('in attentionOnly mode renders only unverifiable', () => {
    const { container: liveBadge } = render(<LivenessBadge verdict="live" attentionOnly />);
    expect(liveBadge).toBeEmptyDOMElement();

    const { container: exitedBadge } = render(<LivenessBadge verdict="exited" attentionOnly />);
    expect(exitedBadge).toBeEmptyDOMElement();

    render(<LivenessBadge verdict="unverifiable" attentionOnly />);
    expect(screen.getByText('Unverifiable')).toBeInTheDocument();
  });

  it('exposes the explanation to screen readers, not only on hover', () => {
    const { container } = render(<LivenessBadge verdict="unverifiable" />);
    expect(container.querySelector('.sr-only')?.textContent).toMatch(/cannot confirm/i);
  });
});

// ---------------------------------------------------------------------------
// The presentation helper
// ---------------------------------------------------------------------------

describe('describeLiveness', () => {
  it('returns a presentation for each known verdict', () => {
    for (const verdict of ['live', 'unverifiable', 'exited'] as const) {
      expect(describeLiveness(verdict)).not.toBeNull();
    }
  });

  it('returns null rather than throwing for unknown and absent values', () => {
    expect(describeLiveness('something_new')).toBeNull();
    expect(describeLiveness(null)).toBeNull();
    expect(describeLiveness(undefined)).toBeNull();
    expect(describeLiveness('')).toBeNull();
  });

  it('never describes unverifiable using exit vocabulary', () => {
    const description = describeLiveness('unverifiable')!.description.toLowerCase();
    for (const forbidden of ['dead', 'ended', 'finished', 'exited', 'failed']) {
      expect(description).not.toContain(forbidden);
    }
  });
});

describe('isAttentionWorthy', () => {
  it('flags only unverifiable', () => {
    expect(isAttentionWorthy('unverifiable')).toBe(true);
    expect(isAttentionWorthy('live')).toBe(false);
    expect(isAttentionWorthy('exited')).toBe(false);
    expect(isAttentionWorthy(null)).toBe(false);
    expect(isAttentionWorthy('unknown_value')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Integration into ActivityCard
// ---------------------------------------------------------------------------

describe('ActivityCard liveness integration', () => {
  it('shows the verdict beside the status, not instead of it', () => {
    renderCard(makeItem({ status: 'in_progress', liveness: 'unverifiable' }));
    // Both must be present — they answer different questions.
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText('Unverifiable')).toBeInTheDocument();
  });

  it('does not badge a healthy in-progress run', () => {
    renderCard(makeItem({ status: 'in_progress', liveness: 'live' }));
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.queryByText('Live')).not.toBeInTheDocument();
  });

  it('announces an unverifiable run to screen readers', () => {
    renderCard(makeItem({ status: 'in_progress', liveness: 'unverifiable' }));
    expect(screen.getByRole('button', { name: /Liveness: unverifiable/i })).toBeInTheDocument();
  });

  it('renders a pre-#4176 row with no verdict unchanged', () => {
    // Rows written before the field existed must keep rendering.
    expect(() => renderCard(makeItem({ status: 'in_progress', liveness: null }))).not.toThrow();
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.queryByText('Unverifiable')).not.toBeInTheDocument();
  });

  it('does not throw when the backend sends a verdict this build does not know', () => {
    expect(() =>
      renderCard(makeItem({ liveness: 'brand_new_verdict' as unknown as null })),
    ).not.toThrow();
  });
});
