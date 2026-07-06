/**
 * NoOrgBanner component tests.
 *
 * Issue #2984: No-org banner for personal/free-tier tenant users.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NoOrgBanner } from '@/components/NoOrgBanner';

describe('NoOrgBanner', () => {
  describe('private App deployment (default)', () => {
    it('shows "ask your org owner" message', () => {
      render(<NoOrgBanner />);

      expect(screen.getByText('Ask your org owner to connect this platform')).toBeInTheDocument();
    });

    it('shows the connect link for copying', () => {
      render(<NoOrgBanner />);

      expect(screen.getByText(/\/settings\/connections/)).toBeInTheDocument();
    });

    it('renders the copy button', () => {
      render(<NoOrgBanner />);

      expect(screen.getByTitle('Copy link')).toBeInTheDocument();
    });

    it('does not show "Connect your repos" CTA', () => {
      render(<NoOrgBanner />);

      expect(screen.queryByText('Connect your repos')).not.toBeInTheDocument();
    });
  });

  describe('public App deployment', () => {
    it('shows "Connect your repos" message', () => {
      render(<NoOrgBanner isPublicApp />);

      expect(screen.getByText('Connect your repos to get started')).toBeInTheDocument();
    });

    it('shows install CTA link when installUrl provided', () => {
      render(<NoOrgBanner isPublicApp installUrl="https://github.com/apps/my-app/installations/new" />);

      const link = screen.getByText('Connect your repos');
      expect(link).toBeInTheDocument();
      expect(link.closest('a')).toHaveAttribute('href', 'https://github.com/apps/my-app/installations/new');
    });

    it('falls back to Go to Connections when no installUrl', () => {
      render(<NoOrgBanner isPublicApp />);

      const link = screen.getByText('Go to Connections');
      expect(link.closest('a')).toHaveAttribute('href', '/settings/connections');
    });

    it('does not show "ask your org owner" copy', () => {
      render(<NoOrgBanner isPublicApp />);

      expect(screen.queryByText('Ask your org owner to connect this platform')).not.toBeInTheDocument();
    });
  });
});
