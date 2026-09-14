/**
 * Tests for NextHome — Issue #5079, updated for #5080.
 *
 * The preview landing page has one job: tell the truth about what the preview is
 * and route people to the current UI for everything else. The assertions below are
 * about that honesty, not about styling.
 *
 * **#5080 change to this file.** Two adjustments, both because the page's role
 * changed rather than because an assertion became inconvenient:
 *
 * 1. The page now renders the Use ADP journey's entry cards from the shared journey
 *    model instead of the standalone `CurrentUiLinks` component, so the stub is the
 *    card list. The honesty assertions it stands in for — every entry labelled
 *    "Opens in the current UI", none pointing into /next — moved to
 *    JourneyEntryCards.test.tsx and are still enforced there and in journeys.test.tsx.
 * 2. #5079's "renders no journey navigation" case is DELETED, not weakened. It
 *    existed to stop placeholder journey nav appearing "until the pages behind it
 *    exist", and it named this story as the point at which that changes. The
 *    navigation now exists, lives in NextLayout, and is real: every entry is gated
 *    and points at a working page. Keeping the case would assert the absence of the
 *    feature this story delivers.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NextHome from '@/pages/next/NextHome';

// The journey model is exercised directly in journeys.test.tsx; here the page's own
// copy is what matters, so the card list is a sentinel.
vi.mock('@/components/next/JourneyEntryCards', () => ({
  JourneyEntryCards: () => <div data-testid="next-entry-cards" />,
}));

vi.mock('@/hooks/useJourneys', () => ({
  useJourneys: () => ({
    journeys: { use: { id: 'use', sections: [] }, admin: { id: 'admin', sections: [] } },
  }),
}));

function renderHome() {
  return render(
    <MemoryRouter>
      <NextHome />
    </MemoryRouter>,
  );
}

describe('NextHome — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the Use ADP journey’s entries for unmigrated capabilities', () => {
    renderHome();
    expect(screen.getByTestId('next-entry-cards')).toBeInTheDocument();
  });

  it('states that the current UI remains the default and still works', () => {
    renderHome();
    expect(screen.getByText(/current UI remains the default/i)).toBeInTheDocument();
  });

  it('points at the return control by name', () => {
    // The control itself lives in NextLayout; the page tells the user it exists.
    renderHome();
    expect(screen.getByText('Back to current UI')).toBeInTheDocument();
  });

  it('does not render the journey navigation itself — that belongs to the layout', () => {
    // The nav is chrome around every preview page, so it lives in NextLayout. A
    // second copy on the home page would double the landmarks and could disagree
    // with the layout's about which journey is active.
    renderHome();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });
});
