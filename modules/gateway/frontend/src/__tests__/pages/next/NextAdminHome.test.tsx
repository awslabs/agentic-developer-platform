/**
 * Tests for NextAdminHome — Issue #5080.
 *
 * The acceptance criteria this page carries are both about honesty rather than
 * function: a system-wide screen must identify its platform scope, and a UI label
 * must never read as a grant of backend authority. Both are easy to regress by
 * editing prose, which is why they are asserted rather than left to review.
 *
 * The entry cards are stubbed: they have their own suite, and what matters here is
 * that this page passes the *administration* journey's sections to them.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NextAdminHome from '@/pages/next/NextAdminHome';
import { buildJourneys } from '@/components/next/journeys';
import { ALL_FEATURES_ENABLED } from '@/services/features';

const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

const mockUsePermissions = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockUsePermissions(),
}));

const mockUseJourneys = vi.fn();
vi.mock('@/hooks/useJourneys', () => ({
  useJourneys: () => mockUseJourneys(),
}));

vi.mock('@/components/next/JourneyEntryCards', () => ({
  JourneyEntryCards: ({
    sections,
    idPrefix,
  }: {
    sections: { title: string }[];
    idPrefix: string;
  }) => (
    <div data-testid="entry-cards" data-prefix={idPrefix}>
      {sections.map((s) => (
        <span key={s.title}>{s.title}</span>
      ))}
    </div>
  ),
}));

function renderPage({
  isPlatformAdmin = false,
  orgId = 'org-1',
}: { isPlatformAdmin?: boolean; orgId?: string } = {}) {
  mockUseAuth.mockReturnValue({ user: { id: 'u-1', orgId } });
  mockUsePermissions.mockReturnValue({ isPlatformAdmin: () => isPlatformAdmin });
  mockUseJourneys.mockReturnValue({
    journeys: buildJourneys(ALL_FEATURES_ENABLED, {
      isPlatformAdmin,
      isOrgAdmin: !isPlatformAdmin,
      canViewOrganizations: true,
      canViewBudgets: true,
      canViewRateLimits: true,
      orgId,
    }),
  });
  return render(
    <MemoryRouter>
      <NextAdminHome />
    </MemoryRouter>,
  );
}

describe('NextAdminHome — Issue #5080', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('the page identifies its scope', () => {
    it('tells a platform admin they administer the whole platform', () => {
      // "System-wide screens identify their platform scope." Without this an org
      // admin and a platform admin see the same heading over different data.
      renderPage({ isPlatformAdmin: true });
      expect(screen.getByTestId('next-admin-scope')).toHaveTextContent('the whole platform');
    });

    it('tells an org admin they administer their own organization', () => {
      renderPage({ isPlatformAdmin: false });
      const scope = screen.getByTestId('next-admin-scope');
      expect(scope).toHaveTextContent('your own organization');
      expect(scope).not.toHaveTextContent('the whole platform');
    });

    it('names the organization an org admin is administering', () => {
      renderPage({ isPlatformAdmin: false, orgId: 'org-42' });
      expect(screen.getByTestId('next-admin-scope')).toHaveTextContent('org-42');
    });

    it('tells an org admin that platform-wide settings are not theirs', () => {
      renderPage({ isPlatformAdmin: false });
      expect(screen.getByTestId('next-admin-scope')).toHaveTextContent(
        'Platform-wide settings are not part of this journey',
      );
    });
  });

  describe('the label is not the authority', () => {
    it('states that the server decides what can be changed', () => {
      // "UI labels/selector values never confer backend authority." This story moves
      // no permission, so the page must not read as a grant.
      renderPage({ isPlatformAdmin: true });
      expect(screen.getByTestId('next-admin-scope')).toHaveTextContent(
        'decided by the server, not by this page',
      );
    });

    it('says so for an org admin as well as a platform admin', () => {
      renderPage({ isPlatformAdmin: false });
      expect(screen.getByTestId('next-admin-scope')).toHaveTextContent(
        'decided by the server',
      );
    });
  });

  describe('capabilities are links, not stubs', () => {
    it('renders the administration journey’s sections', () => {
      renderPage({ isPlatformAdmin: true });
      const cards = screen.getByTestId('entry-cards');
      expect(cards).toHaveTextContent('Organizations & teams');
      expect(cards).toHaveTextContent('Budgets & limits');
    });

    it('says the pages have not moved into the preview yet', () => {
      renderPage({ isPlatformAdmin: true });
      expect(
        screen.getByText(/These pages have not moved into the preview yet/),
      ).toBeInTheDocument();
    });

    it('does not render the Use ADP journey’s sections here', () => {
      // The two journeys are separate groupings of the same pages; duplicating the
      // user entries under Administration would recreate the competing menu tree the
      // criteria rule out.
      renderPage({ isPlatformAdmin: true });
      expect(screen.getByTestId('entry-cards')).not.toHaveTextContent('Your work');
    });
  });
});
