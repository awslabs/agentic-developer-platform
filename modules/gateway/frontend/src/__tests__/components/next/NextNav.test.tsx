/**
 * Tests for NextNav — Issue #5080.
 *
 * The nav renders whatever the gated model gives it, so the gating itself is tested
 * in journeys.test.tsx. What matters here is that it does not misrepresent those
 * entries: an unlabelled link out of the preview reads as a migrated page, and an
 * unlabelled platform-wide entry reads as organization-filtered data.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { NextNav } from '@/components/next/NextNav';
import { buildJourneys, type Journey } from '@/components/next/journeys';
import { ALL_FEATURES_ENABLED } from '@/services/features';

const journeys = buildJourneys(ALL_FEATURES_ENABLED, {
  isPlatformAdmin: true,
  canViewOrganizations: true,
  canViewBudgets: true,
  canViewRateLimits: true,
  canViewLogs: true,
  canViewPool: true,
  canViewMetrics: true,
  orgId: 'org-1',
});

function renderNav(journey: Journey, activeEntryId?: string, onNavigate?: () => void) {
  return render(
    <MemoryRouter>
      <NextNav journey={journey} activeEntryId={activeEntryId} onNavigate={onNavigate} />
    </MemoryRouter>,
  );
}

describe('NextNav — Issue #5080', () => {
  describe('rendering a journey’s entries', () => {
    it('renders every entry of the journey', () => {
      renderNav(journeys.use);
      expect(screen.getByTestId('next-nav-entry-runs')).toHaveAttribute('href', '/runs');
      expect(screen.getByTestId('next-nav-entry-cli-setup')).toHaveAttribute('href', '/setup');
    });

    it('groups entries under their section headings', () => {
      renderNav(journeys.use);
      expect(screen.getByText('Your work')).toBeInTheDocument();
      expect(screen.getByText('Setup & connections')).toBeInTheDocument();
    });

    it('renders the administration journey’s entries when given it', () => {
      renderNav(journeys.admin);
      expect(screen.getByTestId('next-nav-entry-budgets')).toHaveAttribute('href', '/budgets');
    });
  });

  describe('entries are honest about where they lead', () => {
    it('labels every current-UI entry as opening the current UI', () => {
      // This story migrates no capability. Without the label these links read as
      // preview pages that happen to look different — the "misleading live
      // controls" the criteria forbid.
      renderNav(journeys.use);
      const links = screen.getAllByRole('link');
      expect(screen.getAllByText('Opens in the current UI')).toHaveLength(links.length);
    });

    it('never points an entry into the preview', () => {
      for (const journey of [journeys.use, journeys.admin]) {
        const { unmount } = renderNav(journey);
        for (const link of screen.getAllByRole('link')) {
          expect(link.getAttribute('href')).not.toMatch(/^\/next/);
        }
        unmount();
      }
    });
  });

  describe('platform scope is stated, not implied', () => {
    it('labels a platform-wide entry', () => {
      // System health sitting under an organization selector would otherwise imply
      // it shows that organization's health.
      renderNav(journeys.admin);
      const systemHealth = screen.getByTestId('next-nav-entry-system-health');
      expect(systemHealth).toHaveTextContent('Platform-wide');
    });

    it('does not label organization-scoped entries as platform-wide', () => {
      renderNav(journeys.admin);
      expect(screen.getByTestId('next-nav-entry-budgets')).not.toHaveTextContent(
        'Platform-wide',
      );
    });

    it('states the scope as text, so it survives on mobile and in a screen reader', () => {
      // Not a title attribute and not colour.
      renderNav(journeys.admin);
      expect(screen.getAllByText('Platform-wide').length).toBeGreaterThan(0);
    });
  });

  describe('active page state', () => {
    it('marks the active entry with aria-current', () => {
      renderNav(journeys.use, 'runs');
      expect(screen.getByTestId('next-nav-entry-runs')).toHaveAttribute('aria-current', 'page');
    });

    it('marks nothing when no entry matches the current page', () => {
      renderNav(journeys.use);
      expect(screen.queryByRole('link', { current: 'page' })).not.toBeInTheDocument();
    });

    it('matches on entry id, not on destination', () => {
      // Personal Model access and Credentials share /settings/credentials while the
      // capability has not moved yet. Matching on href would light up both and tell
      // the user they are on two pages at once.
      renderNav(journeys.use, 'credentials');
      expect(screen.getByTestId('next-nav-entry-credentials')).toHaveAttribute(
        'aria-current',
        'page',
      );
      expect(
        screen.getByTestId('next-nav-entry-model-access-personal'),
      ).not.toHaveAttribute('aria-current');
    });
  });

  describe('accessibility', () => {
    it('labels the nav landmark with the journey name', () => {
      // So a screen-reader user moving by landmark hears which navigation this is,
      // rather than a second unlabelled "navigation" beside the journey switch.
      renderNav(journeys.use);
      expect(screen.getByRole('navigation', { name: 'Use ADP navigation' })).toBeInTheDocument();
    });

    it('associates each section’s list with its heading', () => {
      renderNav(journeys.use);
      const heading = screen.getByText('Your work');
      expect(heading).toHaveAttribute('id');
      const list = document.querySelector(`ul[aria-labelledby="${heading.getAttribute('id')}"]`);
      expect(list).not.toBeNull();
    });

    it('renders entries as ordinary links in DOM order for Tab traversal', () => {
      // No roving tabindex: a custom key handler here would remove the browser
      // behaviour it imitates.
      renderNav(journeys.use);
      const links = screen.getAllByRole('link');
      expect(links.length).toBeGreaterThan(1);
      for (const link of links) {
        expect(link).not.toHaveAttribute('tabindex');
      }
    });
  });

  describe('mobile drawer integration', () => {
    it('notifies the caller when an entry is followed', () => {
      const onNavigate = vi.fn();
      renderNav(journeys.use, undefined, onNavigate);
      screen.getByTestId('next-nav-entry-runs').click();
      expect(onNavigate).toHaveBeenCalled();
    });
  });
});
