/**
 * Connections page component tests.
 *
 * Issue #477: Test coverage for the Connections page (parent #465).
 * Uses React Testing Library with mocked services.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Connections from '@/pages/settings/Connections';
import { ToastProvider } from '@/contexts/ToastContext';

// Mock the connections service
vi.mock('@/services/connections', () => ({
  startGitHubInstall: vi.fn(),
  listConnections: vi.fn(),
  deleteGitHubConnection: vi.fn(),
  getGitHubAppStatus: vi.fn(),
  startGitHubAppRegistration: vi.fn(),
  rotateGitHubAppKey: vi.fn(),
  disconnectGitHubApp: vi.fn(),
}));

// Connections calls useAuth() to read the current user (free-tier banner gating)
// and hasRole for platform_admin check. Mock both.
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { orgId: 'org-test' }, hasRole: () => false }),
}));

import {
  startGitHubInstall,
  listConnections,
  deleteGitHubConnection,
  getGitHubAppStatus,
} from '@/services/connections';

const mockStartGitHubInstall = startGitHubInstall as ReturnType<typeof vi.fn>;
const mockListConnections = listConnections as ReturnType<typeof vi.fn>;
const mockDeleteGitHubConnection = deleteGitHubConnection as ReturnType<typeof vi.fn>;
const mockGetGitHubAppStatus = getGitHubAppStatus as ReturnType<typeof vi.fn>;

function renderConnections(initialEntries: string[] = ['/settings/connections']) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <ToastProvider>
        <Connections />
      </ToastProvider>
    </MemoryRouter>,
  );
}

describe('Connections Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListConnections.mockResolvedValue({ connections: [] });
    // Default: app is registered so Install button is visible (existing test expectations)
    mockGetGitHubAppStatus.mockResolvedValue({
      registered: true,
      app_slug: 'adp-agent',
      app_id: '123',
      owner_type: 'Organization',
      created_at: '2026-06-01T00:00:00Z',
    });
  });

  describe('Empty state rendering', () => {
    it('renders page header and GitHub tile', async () => {
      renderConnections();

      expect(screen.getByText('Connections')).toBeInTheDocument();
      expect(screen.getByText('GitHub')).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });
    });

    it('shows Coming soon tiles for Slack and Google', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Slack')).toBeInTheDocument();
        expect(screen.getByText('Google')).toBeInTheDocument();
      });
      // Both tiles should have "Coming soon" badge
      const badges = screen.getAllByText('Coming soon');
      expect(badges).toHaveLength(2);
    });

    it('shows no installation cards when connections list is empty', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });
      expect(screen.queryByText('Disconnect')).not.toBeInTheDocument();
    });
  });

  describe('Install flow', () => {
    it('calls startGitHubInstall and redirects to install_url on button click', async () => {
      const user = userEvent.setup();
      const mockUrl = 'https://github.com/apps/adp-agent/installations/new?state=abc';
      mockStartGitHubInstall.mockResolvedValue({
        install_url: mockUrl,
        state_token: 'abc',
        expires_at: '2026-05-05T12:00:00Z',
      });

      // Mock window.location.href setter
      const locationHrefSpy = vi.spyOn(window, 'location', 'get');
      const mockLocation = { ...window.location, href: '' };
      locationHrefSpy.mockReturnValue(mockLocation as Location);

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Install on GitHub'));

      await waitFor(() => {
        expect(mockStartGitHubInstall).toHaveBeenCalledTimes(1);
      });
      expect(mockLocation.href).toBe(mockUrl);

      locationHrefSpy.mockRestore();
    });

    it('shows error toast when startGitHubInstall fails', async () => {
      const user = userEvent.setup();
      mockStartGitHubInstall.mockRejectedValue(new Error('Network error'));

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Install on GitHub'));

      await waitFor(() => {
        expect(screen.getByText('Network error')).toBeInTheDocument();
      });
    });
  });

  describe('Success callback (query param ?success=1)', () => {
    it('shows success toast when ?success=1 is present', async () => {
      renderConnections(['/settings/connections?success=1']);

      await waitFor(() => {
        expect(
          screen.getByText('GitHub connected! You can now trigger agents from this org.'),
        ).toBeInTheDocument();
      });
    });

    it('refetches connections list on success', async () => {
      renderConnections(['/settings/connections?success=1']);

      await waitFor(() => {
        // Initial load + refetch triggered by success param
        expect(mockListConnections).toHaveBeenCalled();
      });
    });
  });

  describe('App registered callback (query param ?github_app=registered)', () => {
    it('shows success toast when login is wired', async () => {
      renderConnections(['/settings/connections?github_app=registered']);

      await waitFor(() => {
        expect(screen.getByText('GitHub App registered successfully!')).toBeInTheDocument();
      });
    });

    it('shows a warning toast when login_enabled=false (Issue #2708)', async () => {
      renderConnections(['/settings/connections?github_app=registered&login_enabled=false']);

      await waitFor(() => {
        expect(
          screen.getByText(/"Sign in with GitHub" is not wired yet/i),
        ).toBeInTheDocument();
      });
    });
  });

  describe('Error callback (query params ?error=...&message=...)', () => {
    it('shows error toast with message from query param', async () => {
      renderConnections(['/settings/connections?error=expired_nonce&message=Token+expired']);

      await waitFor(() => {
        expect(screen.getByText('Token expired')).toBeInTheDocument();
      });
    });

    it('shows fallback error message when message param is missing', async () => {
      renderConnections(['/settings/connections?error=unknown_error']);

      await waitFor(() => {
        expect(
          screen.getByText('GitHub connection failed (unknown_error). Please try again.'),
        ).toBeInTheDocument();
      });
    });
  });

  describe('Connection card rendering', () => {
    const mockConnection = {
      provider: 'github',
      installation_id: 12345,
      account_login: 'my-org',
      account_type: 'Organization',
      repository_selection: 'selected',
      repository_count: 5,
      installed_at: '2026-05-01T10:00:00Z',
      configure_url: 'https://github.com/organizations/my-org/settings/installations/12345',
    };

    beforeEach(() => {
      mockListConnections.mockResolvedValue({ connections: [mockConnection] });
    });

    it('renders org name and installation ID', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('my-org')).toBeInTheDocument();
        expect(screen.getByText(/Installation #12345/)).toBeInTheDocument();
      });
    });

    it('renders repository count', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText(/5 repos/)).toBeInTheDocument();
      });
    });

    it('shows "All repositories" when selection is all', async () => {
      mockListConnections.mockResolvedValue({
        connections: [{ ...mockConnection, repository_selection: 'all' }],
      });

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText(/All repositories/)).toBeInTheDocument();
      });
    });

    it('renders Configure on GitHub link pointing to correct URL', async () => {
      renderConnections();

      await waitFor(() => {
        const link = screen.getByText(/Configure on GitHub/);
        expect(link).toBeInTheDocument();
        expect(link.closest('a')).toHaveAttribute('href', mockConnection.configure_url);
        expect(link.closest('a')).toHaveAttribute('target', '_blank');
      });
    });
  });

  describe('Disconnect flow', () => {
    const mockConnection = {
      provider: 'github',
      installation_id: 12345,
      account_login: 'my-org',
      account_type: 'Organization',
      repository_selection: 'selected',
      repository_count: 3,
      installed_at: '2026-05-01T10:00:00Z',
      configure_url: 'https://github.com/organizations/my-org/settings/installations/12345',
    };

    beforeEach(() => {
      mockListConnections.mockResolvedValue({ connections: [mockConnection] });
      mockDeleteGitHubConnection.mockResolvedValue({
        deleted: true,
        installation_id: 12345,
      });
    });

    it('shows Disconnect button on card', async () => {
      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Disconnect')).toBeInTheDocument();
      });
    });

    it('first click shows Confirm, second click calls deleteGitHubConnection', async () => {
      const user = userEvent.setup();

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Disconnect')).toBeInTheDocument();
      });

      // First click — shows confirmation
      await user.click(screen.getByText('Disconnect'));
      expect(screen.getByText('Confirm?')).toBeInTheDocument();

      // After delete, the list should refresh with empty result
      mockListConnections.mockResolvedValue({ connections: [] });

      // Second click — performs delete
      await user.click(screen.getByText('Confirm?'));

      await waitFor(() => {
        expect(mockDeleteGitHubConnection).toHaveBeenCalledWith(12345);
      });
    });

    it('shows success toast after disconnect', async () => {
      const user = userEvent.setup();

      renderConnections();

      await waitFor(() => {
        expect(screen.getByText('Disconnect')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Disconnect'));
      await user.click(screen.getByText('Confirm?'));

      await waitFor(() => {
        expect(screen.getByText('GitHub installation disconnected.')).toBeInTheDocument();
      });
    });
  });
});
