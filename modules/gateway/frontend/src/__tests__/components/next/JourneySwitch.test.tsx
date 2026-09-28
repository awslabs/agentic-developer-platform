/**
 * Tests for JourneySwitch — Issue #5080.
 *
 * The switch is where "members see Use ADP; administrators can enter
 * Administration" becomes visible behaviour, and where two mistakes would be easy:
 * rendering a dead Administration tab for someone who cannot administer, and
 * replacing the outer "Back to current UI" control with this one.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { JourneySwitch } from '@/components/next/JourneySwitch';
import { buildJourneys, type Journey, type JourneyId } from '@/components/next/journeys';
import { ALL_FEATURES_ENABLED } from '@/services/features';

const journeys = buildJourneys(ALL_FEATURES_ENABLED, {
  isPlatformAdmin: true,
  canViewOrganizations: true,
  canViewBudgets: true,
  canViewRateLimits: true,
  orgId: 'org-1',
});

function renderSwitch(
  available: Journey[],
  active: JourneyId = 'use',
  destination: (journey: Journey) => string = (j) => j.home,
  onNavigate?: () => void,
) {
  return render(
    <MemoryRouter>
      <JourneySwitch
        journeys={available}
        active={active}
        destination={destination}
        onNavigate={onNavigate}
      />
    </MemoryRouter>,
  );
}

describe('JourneySwitch — Issue #5080', () => {
  describe('offering the journeys', () => {
    it('shows both journeys to an actor who can administer', () => {
      renderSwitch([journeys.use, journeys.admin]);
      expect(screen.getByTestId('next-journey-use')).toHaveTextContent('Use ADP');
      expect(screen.getByTestId('next-journey-admin')).toHaveTextContent('Administration');
    });

    it('renders nothing at all when only one journey is available', () => {
      // A member must not see a disabled or dead Administration tab: that is a
      // nonfunctional control presented as a feature, and it implies an
      // administration surface they do not have. A lone "Use ADP" tab is also
      // meaningless, so the whole control is withheld.
      renderSwitch([journeys.use]);
      expect(screen.queryByTestId('next-journey-switch')).not.toBeInTheDocument();
      expect(screen.queryByText('Administration')).not.toBeInTheDocument();
    });
  });

  describe('journeys are real destinations, not in-page state', () => {
    it('links each journey to its home by default', () => {
      renderSwitch([journeys.use, journeys.admin]);
      expect(screen.getByTestId('next-journey-use')).toHaveAttribute('href', '/next');
      expect(screen.getByTestId('next-journey-admin')).toHaveAttribute('href', '/next/admin');
    });

    it('links to a remembered location when the caller supplies one', () => {
      // The per-journey return location: leaving Administration should come back to
      // where you were in Use ADP, not to its home page.
      renderSwitch([journeys.use, journeys.admin], 'admin', (j) =>
        j.id === 'use' ? '/next/remembered' : j.home,
      );
      expect(screen.getByTestId('next-journey-use')).toHaveAttribute(
        'href',
        '/next/remembered',
      );
    });

    it('renders anchors so bookmarking and middle-click work', () => {
      renderSwitch([journeys.use, journeys.admin]);
      expect(screen.getAllByRole('link')).toHaveLength(2);
    });
  });

  describe('active state is conveyed to assistive technology', () => {
    it('marks the active journey with aria-current', () => {
      renderSwitch([journeys.use, journeys.admin], 'admin');
      expect(screen.getByTestId('next-journey-admin')).toHaveAttribute('aria-current', 'page');
    });

    it('does not mark the inactive journey', () => {
      // Colour alone would not tell a screen-reader user which journey they are in.
      renderSwitch([journeys.use, journeys.admin], 'admin');
      expect(screen.getByTestId('next-journey-use')).not.toHaveAttribute('aria-current');
    });

    it('labels the switch as its own landmark', () => {
      // Distinguishable from the journey's own navigation list, which is labelled
      // with the journey name.
      renderSwitch([journeys.use, journeys.admin]);
      expect(screen.getByRole('navigation', { name: 'Journey' })).toBeInTheDocument();
    });
  });

  describe('it is not the return link', () => {
    it('offers no way out of the preview', () => {
      // "Back to current UI" is a separate, persistent control in NextLayout. If
      // this component ever grew one, the two would compete and #5079's contract
      // that both are present would be broken.
      renderSwitch([journeys.use, journeys.admin]);
      expect(screen.queryByText(/back to current ui/i)).not.toBeInTheDocument();
      for (const link of screen.getAllByRole('link')) {
        expect(link.getAttribute('href')).toMatch(/^\/next/);
      }
    });
  });

  describe('mobile drawer integration', () => {
    it('notifies the caller when a journey is followed', () => {
      // The drawer closes on navigate; a drawer left open would cover the page the
      // user just asked for.
      const onNavigate = vi.fn();
      renderSwitch([journeys.use, journeys.admin], 'use', (j) => j.home, onNavigate);
      screen.getByTestId('next-journey-admin').click();
      expect(onNavigate).toHaveBeenCalled();
    });
  });
});
