/**
 * GitHubTile component tests.
 *
 * Issue #2596: GitHub App registration + lifecycle states (platform_admin gated).
 *
 * Tests cover the four tile states:
 * - platform_admin + unregistered → "Set up GitHub App" with owner radio
 * - platform_admin + registered → Rotate/Disconnect + Connect UI
 * - non-platform_admin + unregistered → "ask a platform admin" message
 * - any admin + registered → Connect flow unchanged
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { GitHubTile } from '@/pages/settings/components/GitHubTile';
import type { AppStatusResponse, GitHubConnectionItem } from '@/services/connections';

// Default props for a minimal render
const defaultProps = {
  connections: [] as GitHubConnectionItem[],
  isLoading: false,
  onInstall: vi.fn(),
  onDisconnect: vi.fn().mockResolvedValue(undefined),
  isInstalling: false,
  isPlatformAdmin: false,
  appStatus: null as AppStatusResponse | null,
  onRegister: vi.fn().mockResolvedValue(undefined),
  onRotateKey: vi.fn().mockResolvedValue(undefined),
  onDisconnectApp: vi.fn().mockResolvedValue(undefined),
};

const registeredStatus: AppStatusResponse = {
  registered: true,
  app_slug: 'adp-agent-dev',
  app_id: '12345',
  owner_type: 'Organization',
  created_at: '2026-06-15T10:00:00Z',
};

const unregisteredStatus: AppStatusResponse = {
  registered: false,
  app_slug: null,
  app_id: null,
  owner_type: null,
  created_at: null,
};

const mockConnection: GitHubConnectionItem = {
  provider: 'github',
  installation_id: 99999,
  account_login: 'test-org',
  account_type: 'Organization',
  repository_selection: 'selected',
  repository_count: 3,
  repositories: ['repo-a', 'repo-b', 'repo-c'],
  installed_at: '2026-06-20T10:00:00Z',
  configure_url: 'https://github.com/organizations/test-org/settings/installations/99999',
};

describe('GitHubTile', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // -------------------------------------------------------------------------
  // State 1: platform_admin + unregistered
  // -------------------------------------------------------------------------

  describe('platform_admin + unregistered', () => {
    const props = {
      ...defaultProps,
      isPlatformAdmin: true,
      appStatus: unregisteredStatus,
    };

    it('shows "Set up GitHub App" info box', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByText('Set up GitHub App')).toBeInTheDocument();
    });

    it('shows owner type radio with user-owned default', () => {
      render(<GitHubTile {...props} />);

      const userRadio = screen.getByLabelText(/Personal account/i);
      const orgRadio = screen.getByLabelText(/Organization/i);

      expect(userRadio).toBeChecked();
      expect(orgRadio).not.toBeChecked();
    });

    it('does NOT show org name input when user-owned is selected', () => {
      render(<GitHubTile {...props} />);

      expect(screen.queryByLabelText(/Organization name/i)).not.toBeInTheDocument();
    });

    it('shows org name input when Organization radio is selected', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      await user.click(screen.getByLabelText(/Organization/i));

      expect(screen.getByLabelText(/Organization name/i)).toBeInTheDocument();
    });

    it('calls onRegister with owner_type=user when Create button is clicked', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      await user.click(screen.getByRole('button', { name: /Create on GitHub/i }));

      await waitFor(() => {
        expect(props.onRegister).toHaveBeenCalledWith('user', undefined);
      });
    });

    it('calls onRegister with owner_type=org and org name when org is chosen', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      await user.click(screen.getByLabelText(/Organization/i));
      await user.type(screen.getByLabelText(/Organization name/i), 'my-github-org');
      await user.click(screen.getByRole('button', { name: /Create on GitHub/i }));

      await waitFor(() => {
        expect(props.onRegister).toHaveBeenCalledWith('org', 'my-github-org');
      });
    });

    it('disables Create button when org is selected but name is empty', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      await user.click(screen.getByLabelText(/Organization/i));

      expect(screen.getByRole('button', { name: /Create on GitHub/i })).toBeDisabled();
    });

    it('does NOT show Install on GitHub button when unregistered', () => {
      render(<GitHubTile {...props} />);

      expect(screen.queryByText('Install on GitHub')).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // State 2: platform_admin + registered
  // -------------------------------------------------------------------------

  describe('platform_admin + registered', () => {
    const props = {
      ...defaultProps,
      isPlatformAdmin: true,
      appStatus: registeredStatus,
    };

    it('shows app slug and ID', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByText('adp-agent-dev')).toBeInTheDocument();
      expect(screen.getByText(/ID: 12345/)).toBeInTheDocument();
    });

    it('shows owner type in app info', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByText(/Organization/)).toBeInTheDocument();
    });

    it('shows Rotate key button', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByRole('button', { name: /Rotate key/i })).toBeInTheDocument();
    });

    it('shows Disconnect app button', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByRole('button', { name: /Disconnect app/i })).toBeInTheDocument();
    });

    it('calls onRotateKey when Rotate key is clicked', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      await user.click(screen.getByRole('button', { name: /Rotate key/i }));

      await waitFor(() => {
        expect(props.onRotateKey).toHaveBeenCalledTimes(1);
      });
    });

    it('requires confirmation for Disconnect app', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      // First click — changes to confirm
      await user.click(screen.getByRole('button', { name: /Disconnect app/i }));
      expect(screen.getByRole('button', { name: /Confirm disconnect/i })).toBeInTheDocument();
      expect(props.onDisconnectApp).not.toHaveBeenCalled();

      // Second click — calls the handler
      await user.click(screen.getByRole('button', { name: /Confirm disconnect/i }));

      await waitFor(() => {
        expect(props.onDisconnectApp).toHaveBeenCalledTimes(1);
      });
    });

    it('shows Install on GitHub button', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
    });

    it('shows existing connections when present', () => {
      render(<GitHubTile {...props} connections={[mockConnection]} />);

      expect(screen.getByText('test-org')).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // State 3: non-platform_admin + unregistered
  // -------------------------------------------------------------------------

  describe('non-platform_admin + unregistered', () => {
    const props = {
      ...defaultProps,
      isPlatformAdmin: false,
      appStatus: unregisteredStatus,
    };

    it('shows "ask a platform admin" message', () => {
      render(<GitHubTile {...props} />);

      expect(
        screen.getByText(/ask a platform admin/i),
      ).toBeInTheDocument();
    });

    it('does NOT show registration form', () => {
      render(<GitHubTile {...props} />);

      expect(screen.queryByText('Set up GitHub App')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Create on GitHub/i })).not.toBeInTheDocument();
    });

    it('does NOT show Install on GitHub button', () => {
      render(<GitHubTile {...props} />);

      expect(screen.queryByText('Install on GitHub')).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // State 4: non-platform_admin + registered (existing Connect flow)
  // -------------------------------------------------------------------------

  describe('non-platform_admin + registered', () => {
    const props = {
      ...defaultProps,
      isPlatformAdmin: false,
      appStatus: registeredStatus,
    };

    it('shows Install on GitHub button', () => {
      render(<GitHubTile {...props} />);

      expect(screen.getByText('Install on GitHub')).toBeInTheDocument();
    });

    it('does NOT show app info panel (admin-only)', () => {
      render(<GitHubTile {...props} />);

      expect(screen.queryByText('adp-agent-dev')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Rotate key/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Disconnect app/i })).not.toBeInTheDocument();
    });

    it('calls onInstall when Install on GitHub is clicked', async () => {
      const user = userEvent.setup();
      render(<GitHubTile {...props} />);

      await user.click(screen.getByText('Install on GitHub'));

      expect(props.onInstall).toHaveBeenCalledTimes(1);
    });

    it('shows existing connections', () => {
      render(<GitHubTile {...props} connections={[mockConnection]} />);

      expect(screen.getByText('test-org')).toBeInTheDocument();
    });

    it('shows "+ Add another connection" when connections exist', () => {
      render(<GitHubTile {...props} connections={[mockConnection]} />);

      expect(screen.getByText('+ Add another connection')).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------

  describe('loading state', () => {
    it('shows skeleton when loading and no status available', () => {
      render(<GitHubTile {...defaultProps} isLoading={true} appStatus={null} />);

      // The skeleton is a pulsing div — no text content to query.
      // We verify the registration form and message are NOT shown.
      expect(screen.queryByText('Set up GitHub App')).not.toBeInTheDocument();
      expect(screen.queryByText(/ask a platform admin/i)).not.toBeInTheDocument();
    });
  });
});
