/**
 * Onboarding verification UI tests — Issue #4016.
 *
 * The card used to render a hardcoded green "Installed ✓" that meant only "a
 * database row exists". These tests pin the three behaviours that make it
 * honest:
 *   1. a broken check is visible, and says what breaks for the user
 *   2. an UNKNOWN check renders amber, never red (no false-negative reds)
 *   3. a fully-healthy install looks exactly as it did before (no new noise)
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GitHubTile } from '@/pages/settings/components/GitHubTile';
import { InstallationCard } from '@/pages/settings/components/InstallationCard';
import type {
  AppStatusResponse,
  ConnectionVerification,
  GitHubConnectionItem,
  PlatformVerification,
} from '@/services/connections';

const healthyVerification: ConnectionVerification = {
  record_present: true,
  tenant_secret_seeded: true,
  identity_index_row: true,
  reverse_identity_row: true,
};

function connection(
  verification?: ConnectionVerification | null,
  overrides: Partial<GitHubConnectionItem> = {},
): GitHubConnectionItem {
  return {
    provider: 'github',
    installation_id: 99999,
    account_login: 'test-org',
    account_type: 'Organization',
    repository_selection: 'selected',
    repository_count: 1,
    repositories: ['repo-a'],
    installed_at: '2026-08-01T10:00:00Z',
    configure_url: 'https://github.com/settings/installations/99999',
    manage_url: 'https://github.com/settings/installations/99999',
    can_manage: true,
    verification,
    ...overrides,
  };
}

const cardProps = {
  onDisconnect: vi.fn().mockResolvedValue(undefined),
};

describe('InstallationCard verification (Issue #4016)', () => {
  it('shows the plain Installed badge when every check passes', () => {
    render(<InstallationCard connection={connection(healthyVerification)} {...cardProps} />);

    expect(screen.getByText(/Installed/)).toBeInTheDocument();
    expect(screen.queryByText(/Needs attention/)).not.toBeInTheDocument();
    // A healthy install must not sprout a checklist.
    expect(screen.queryByText(/Agent credentials/)).not.toBeInTheDocument();
  });

  it('flags a missing tenant secret and says what it costs the user', () => {
    render(
      <InstallationCard
        connection={connection({ ...healthyVerification, tenant_secret_seeded: false })}
        {...cardProps}
      />,
    );

    expect(screen.getByText(/Needs attention/)).toBeInTheDocument();
    expect(screen.getByText(/Agent credentials/)).toBeInTheDocument();
    expect(screen.getByText(/fail on startup/i)).toBeInTheDocument();
  });

  it('flags broken webhook routing', () => {
    render(
      <InstallationCard
        connection={connection({ ...healthyVerification, identity_index_row: false })}
        {...cardProps}
      />,
    );

    expect(screen.getByText(/Needs attention/)).toBeInTheDocument();
    expect(screen.getByText(/labels and @-mentions will be ignored/i)).toBeInTheDocument();
  });

  it('renders an UNKNOWN check as partly-verified, not as broken', () => {
    render(
      <InstallationCard
        connection={connection({ ...healthyVerification, tenant_secret_seeded: null })}
        {...cardProps}
      />,
    );

    // Amber, not red: a check that errored is not a check that failed.
    expect(screen.getByText(/Partly verified/)).toBeInTheDocument();
    expect(screen.queryByText(/Needs attention/)).not.toBeInTheDocument();
    expect(screen.getByText(/could not be completed/i)).toBeInTheDocument();
  });

  it('surfaces a DynamoDB-only orphan as unrecorded', () => {
    render(
      <InstallationCard
        connection={connection({ ...healthyVerification, record_present: false })}
        {...cardProps}
      />,
    );

    expect(screen.getByText(/Recorded in this workspace/)).toBeInTheDocument();
    expect(screen.getByText(/no record of it/i)).toBeInTheDocument();
  });

  it('keeps the old badge when the API sends no verification block', () => {
    // Backwards compatibility: an older API response must not render a scary
    // invented state.
    render(<InstallationCard connection={connection(undefined)} {...cardProps} />);

    expect(screen.getByText(/Installed/)).toBeInTheDocument();
    expect(screen.queryByText(/Partly verified/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Needs attention/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Platform-scoped panel
// ---------------------------------------------------------------------------

const registeredStatus: AppStatusResponse = {
  registered: true,
  install_ready: true,
  login_enabled: true,
  app_slug: 'adp-agent-dev',
  app_id: '12345',
  owner_type: 'Organization',
  created_at: '2026-06-15T10:00:00Z',
};

const tileProps = {
  connections: [] as GitHubConnectionItem[],
  isLoading: false,
  onInstall: vi.fn(),
  onDisconnect: vi.fn().mockResolvedValue(undefined),
  isInstalling: false,
  isPlatformAdmin: true,
  appStatus: registeredStatus,
  onRegister: vi.fn().mockResolvedValue(undefined),
  onRotateKey: vi.fn().mockResolvedValue(undefined),
  onDisconnectApp: vi.fn().mockResolvedValue(undefined),
};

describe('GitHubTile platform verification (Issue #4016)', () => {
  it('renders nothing when the API omitted the platform block', () => {
    // Non-admins get no platform_verification at all. Absence means "not shown
    // to me", so the panel must not appear — and must not imply health either.
    render(<GitHubTile {...tileProps} platformVerification={null} />);

    expect(screen.queryByText(/not fully wired for GitHub/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Webhook signing secret/)).not.toBeInTheDocument();
  });

  it('renders nothing when every platform check passes', () => {
    const healthy: PlatformVerification = { login_credentials: true, webhook_secret: true };
    render(<GitHubTile {...tileProps} platformVerification={healthy} />);

    expect(screen.queryByText(/not fully wired for GitHub/i)).not.toBeInTheDocument();
  });

  it('reports a placeholder webhook secret as a deployment problem', () => {
    const broken: PlatformVerification = { login_credentials: true, webhook_secret: false };
    render(<GitHubTile {...tileProps} platformVerification={broken} />);

    expect(screen.getByText(/not fully wired for GitHub/i)).toBeInTheDocument();
    expect(screen.getByText(/deliveries will be rejected/i)).toBeInTheDocument();
  });

  it('reports missing login credentials', () => {
    const broken: PlatformVerification = { login_credentials: false, webhook_secret: true };
    render(<GitHubTile {...tileProps} platformVerification={broken} />);

    expect(screen.getByText(/Sign in with GitHub” will fail for everyone/i)).toBeInTheDocument();
  });

  it('uses the softer wording when checks are merely unverified', () => {
    const unknown: PlatformVerification = { login_credentials: null, webhook_secret: true };
    render(<GitHubTile {...tileProps} platformVerification={unknown} />);

    expect(screen.getByText(/could not be verified/i)).toBeInTheDocument();
    expect(screen.queryByText(/not fully wired for GitHub/i)).not.toBeInTheDocument();
  });
});
