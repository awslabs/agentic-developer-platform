/**
 * Unit tests for DraftPanel (#4208).
 *
 * The panel is the user-visible half of the intent-intake loop: if the agent
 * calls `update_draft` and nothing appears here, the feature is invisible. These
 * tests cover what renders, what is hidden, and — the load-bearing part — that a
 * STATE_DELTA patch (top-level AND nested) actually lands in the panel.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DraftPanel } from '@/components/chat/DraftPanel';
import { applyPatches } from '@/utils/jsonPatch';
import type { IntentDraft, SessionMeta } from '@/types/ag-ui-events';

const FULL_DRAFT: IntentDraft = {
  intent: 'Nightly cost report emailed to the platform team',
  motivation: 'Nobody notices spend spikes until the monthly invoice',
  outcomes: ['Email at 08:00 UTC', 'Per-account breakdown'],
  constraints: ['No new AWS accounts', 'Must reuse the existing SES identity'],
  openQuestions: ['Which accounts are in scope?'],
  updatedAt: '2026-08-28T10:00:00.000Z',
};

describe('DraftPanel', () => {
  it('renders nothing when no draft has been produced yet', () => {
    const { container } = render(<DraftPanel />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing for a draft with no populated fields', () => {
    // The agent cleared the draft: a visible empty box would be worse than none.
    const { container } = render(<DraftPanel draft={{ updatedAt: 'x' }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders every populated field', () => {
    render(<DraftPanel draft={FULL_DRAFT} />);

    expect(screen.getByText(FULL_DRAFT.intent!)).toBeInTheDocument();
    expect(screen.getByText(FULL_DRAFT.motivation!)).toBeInTheDocument();
    expect(screen.getByText('Email at 08:00 UTC')).toBeInTheDocument();
    expect(screen.getByText('Per-account breakdown')).toBeInTheDocument();
    expect(screen.getByText('No new AWS accounts')).toBeInTheDocument();
    expect(screen.getByText('Which accounts are in scope?')).toBeInTheDocument();
  });

  it('shows a partial draft without placeholder sections for missing fields', () => {
    // The persona is told an empty draft with one honest field beats five
    // speculative ones, so a one-field draft must render cleanly.
    render(<DraftPanel draft={{ intent: 'Just the headline so far' }} />);

    expect(screen.getByText('Just the headline so far')).toBeInTheDocument();
    expect(screen.queryByText('Motivation')).not.toBeInTheDocument();
    expect(screen.queryByText('Outcomes')).not.toBeInTheDocument();
    expect(screen.queryByText('Open questions')).not.toBeInTheDocument();
  });

  it('renders duplicate list items rather than collapsing them', () => {
    render(<DraftPanel draft={{ outcomes: ['same', 'same'] }} />);
    expect(screen.getAllByText('same')).toHaveLength(2);
  });

  // -------------------------------------------------------------------------
  // The STATE_DELTA path — patch → panel
  // -------------------------------------------------------------------------

  it('renders a draft delivered by a top-level STATE_DELTA patch', () => {
    // What update_draft actually emits: one whole-object replace at /draft.
    const meta = applyPatches({} as Record<string, unknown>, [
      { op: 'replace', path: '/draft', value: FULL_DRAFT },
    ]) as SessionMeta;

    render(<DraftPanel draft={meta.draft} />);
    expect(screen.getByText(FULL_DRAFT.intent!)).toBeInTheDocument();
  });

  it('renders a draft built up by NESTED STATE_DELTA patches', () => {
    // Before #4208 a nested pointer was applied as a literal key named
    // "draft/intent" and silently dropped, so the panel stayed blank. Any
    // future producer emitting field-level ops must still render.
    const meta = applyPatches({} as Record<string, unknown>, [
      { op: 'add', path: '/draft/intent', value: 'Nested intent' },
      { op: 'add', path: '/draft/outcomes', value: ['first'] },
      { op: 'add', path: '/draft/outcomes/-', value: 'second' },
    ]) as SessionMeta;

    render(<DraftPanel draft={meta.draft} />);
    expect(screen.getByText('Nested intent')).toBeInTheDocument();
    expect(screen.getByText('first')).toBeInTheDocument();
    expect(screen.getByText('second')).toBeInTheDocument();
  });

  it('hides the panel again when a patch removes the draft', () => {
    const meta = applyPatches({ draft: FULL_DRAFT } as Record<string, unknown>, [
      { op: 'remove', path: '/draft' },
    ]) as SessionMeta;

    const { container } = render(<DraftPanel draft={meta.draft} />);
    expect(container).toBeEmptyDOMElement();
  });
});
