/**
 * Tests for JourneyEntryCards — Issue #5080.
 *
 * The orientation view both journey home pages render. Its gating comes from the
 * model, so what is asserted here is the honesty of the presentation: every entry
 * that leaves the preview says so, platform-wide data says so, and the links are
 * client-side so following one keeps the shared session and organization context.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

vi.mock('@/services/auth', () => ({ getAccessToken: vi.fn(() => null) }));
import { JourneyEntryCards } from '@/components/next/JourneyEntryCards';
import type { JourneySection } from '@/components/next/journeys';

const SECTIONS: JourneySection[] = [
  {
    title: 'Organizations & teams',
    entries: [
      {
        id: 'organizations',
        to: '/admin/organizations',
        label: 'Organizations & teams',
        description: 'Organization structure.',
        currentUi: true,
      },
    ],
  },
  {
    title: 'System',
    entries: [
      {
        id: 'system-health',
        to: '/admin/system',
        label: 'System health',
        description: 'Platform-wide health.',
        currentUi: true,
        scope: 'platform',
      },
    ],
  },
];

function renderCards(sections: JourneySection[] = SECTIONS, idPrefix = 'test') {
  return render(
    <MemoryRouter>
      <JourneyEntryCards sections={sections} idPrefix={idPrefix} />
    </MemoryRouter>,
  );
}

describe('JourneyEntryCards — Issue #5080', () => {
  describe('rendering entries', () => {
    it('renders a card per entry, linked to its destination', () => {
      renderCards();
      expect(screen.getByTestId('next-entry-card-organizations')).toHaveAttribute(
        'href',
        '/admin/organizations',
      );
      expect(screen.getByTestId('next-entry-card-system-health')).toHaveAttribute(
        'href',
        '/admin/system',
      );
    });

    it('shows each entry’s description, not just its label', () => {
      // This is the orientation view: a first-time visitor should be able to tell
      // what a capability is before following the link.
      renderCards();
      expect(screen.getByText('Organization structure.')).toBeInTheDocument();
    });

    it('groups entries under their section headings', () => {
      renderCards();
      expect(screen.getByText('System')).toBeInTheDocument();
    });

    it('renders nothing but its container when there are no sections', () => {
      // A journey can gate down to nothing; the page must not show an empty heading.
      renderCards([]);
      expect(screen.getByTestId('next-entry-cards')).toBeEmptyDOMElement();
    });
  });

  describe('labels are honest', () => {
    it('labels every current-UI entry', () => {
      renderCards();
      expect(screen.getAllByText('Opens in the current UI')).toHaveLength(2);
    });

    it('omits the label for an entry that does not leave the preview', () => {
      // Nothing sets this today — every entry is currentUi. Asserted so the label is
      // known to be driven by the flag rather than printed unconditionally, which
      // would silently mislabel the first migrated page.
      renderCards([
        {
          title: 'Preview',
          entries: [
            {
              id: 'native',
              to: '/next/native',
              label: 'A migrated page',
              description: 'Lives in the preview.',
              currentUi: false,
            },
          ],
        },
      ]);
      expect(screen.queryByText('Opens in the current UI')).not.toBeInTheDocument();
    });

    it('marks platform-wide entries and only those', () => {
      renderCards();
      expect(screen.getByTestId('next-entry-card-system-health')).toHaveTextContent(
        'Platform-wide',
      );
      expect(screen.getByTestId('next-entry-card-organizations')).not.toHaveTextContent(
        'Platform-wide',
      );
    });
  });

  describe('navigation stays inside the SPA', () => {
    it('uses client-side links', () => {
      // A raw <a> would hard-reload and drop the shared session, active organization
      // and query cache that #5079 established as one shared context.
      renderCards();
      const card = screen.getByTestId('next-entry-card-organizations');
      expect(card.tagName).toBe('A');
      expect(card).not.toHaveAttribute('target');
      expect(card).not.toHaveAttribute('rel');
    });
  });

  describe('accessibility', () => {
    it('names each section as its own region', () => {
      // The section is labelled by its heading, so a screen-reader user moving by
      // region hears "Organizations & teams" rather than an unnamed group of links.
      renderCards();
      const heading = screen.getByText('System');
      expect(heading.id).toBeTruthy();
      const region = document.querySelector(`section[aria-labelledby="${heading.id}"]`);
      expect(region).not.toBeNull();
      expect(region).toContainElement(screen.getByTestId('next-entry-card-system-health'));
    });

    it('scopes heading ids by prefix so two card lists can coexist', () => {
      // Both home pages use this component; duplicate ids would break the
      // heading/list association that screen readers rely on.
      const first = renderCards(SECTIONS, 'page-a');
      const idsA = Array.from(document.querySelectorAll('h3')).map((h) => h.id);
      first.unmount();

      renderCards(SECTIONS, 'page-b');
      const idsB = Array.from(document.querySelectorAll('h3')).map((h) => h.id);
      expect(idsA.length).toBeGreaterThan(0);
      for (const id of idsA) expect(idsB).not.toContain(id);
    });
  });

  describe('a server-owned path must leave the SPA — #5123', () => {
    const EXTERNAL: JourneySection[] = [
      {
        title: 'Setup & connections',
        entries: [
          {
            id: 'gitlab',
            to: '/gitlab/',
            label: 'GitLab',
            description: 'Open the connected GitLab instance.',
            currentUi: true,
            external: true,
          },
          {
            id: 'cli-setup',
            to: '/setup',
            label: 'CLI setup',
            description: 'Set up Codex or Claude Code.',
            currentUi: true,
          },
        ],
      },
    ];

    function renderWithLocation(sections: JourneySection[]) {
      function Probe() {
        return <span data-testid="location">{useLocation().pathname}</span>;
      }
      return render(
        <MemoryRouter initialEntries={['/next']}>
          <JourneyEntryCards sections={sections} idPrefix="home" />
          <Routes>
            <Route path="*" element={<Probe />} />
          </Routes>
        </MemoryRouter>,
      );
    }

    it('renders an external entry as a real anchor, not a router link', () => {
      renderWithLocation(EXTERNAL);
      const card = screen.getByTestId('next-entry-card-gitlab');
      expect(card.tagName).toBe('A');
      expect(card).toHaveAttribute('href', '/gitlab/');
      // A router Link would push client-side, match nothing and land on the
      // /next catch-all 404 instead of the real page.
      fireEvent.click(card);
      expect(screen.getByTestId('location')).toHaveTextContent('/next');
    });

    it('still renders an ordinary entry as a client-side link', () => {
      // Scope check: staying in the SPA is what keeps identity, the active
      // organization and the query cache shared between the two UIs.
      renderWithLocation(EXTERNAL);
      fireEvent.click(screen.getByTestId('next-entry-card-cli-setup'));
      expect(screen.getByTestId('location')).toHaveTextContent('/setup');
    });
  });
});