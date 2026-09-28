/**
 * GitHub App config drift UI tests — Issue #4017.
 *
 * ADP validated the App's settings once, at registration, and never again.
 * GitHub fires no event when an admin edits them, so drift surfaced as a dead
 * login button or agents that silently never fired.
 *
 * The two behaviours these tests pin:
 *   1. drift is visible and says what it costs the user — but UNKNOWN renders
 *      amber, never red (a false-negative red sends operators to fix nothing)
 *   2. the callback URL is REPORTED, never diffed. GitHub exposes no API to read
 *      an App's callback URL back, so rendering it as a pass/fail check would
 *      produce permanent false-positive drift.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { GitHubTile } from '@/pages/settings/components/GitHubTile';
import type {
  AppStatusResponse,
  GitHubConnectionItem,
  PlatformVerification,
} from '@/services/connections';

const registeredStatus: AppStatusResponse = {
  registered: true,
  login_enabled: true,
  app_slug: 'adp-agent-dev',
  app_id: '12345',
  owner_type: 'Organization',
  created_at: '2026-06-15T10:00:00Z',
};

function tileProps(overrides: Record<string, unknown> = {}) {
  return {
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
    ...overrides,
  };
}

const allHealthy: PlatformVerification = {
  login_credentials: true,
  webhook_secret: true,
  app_webhook_url_matches: true,
  app_permissions_match: true,
  app_events_match: true,
};

describe('App config drift rows (Issue #4017)', () => {
  it('stays silent when the App configuration matches', () => {
    render(<GitHubTile {...tileProps()} platformVerification={allHealthy} />);

    expect(screen.queryByText(/App webhook URL/)).not.toBeInTheDocument();
    expect(screen.queryByText(/App permissions/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not fully wired for GitHub/i)).not.toBeInTheDocument();
  });

  it('reports a drifted webhook URL and what it costs the user', () => {
    render(
      <GitHubTile
        {...tileProps()}
        platformVerification={{ ...allHealthy, app_webhook_url_matches: false }}
      />,
    );

    expect(screen.getByText(/not fully wired for GitHub/i)).toBeInTheDocument();
    expect(screen.getByText(/no agent will ever be triggered/i)).toBeInTheDocument();
  });

  it('reports revoked permissions', () => {
    render(
      <GitHubTile
        {...tileProps()}
        platformVerification={{ ...allHealthy, app_permissions_match: false }}
      />,
    );

    expect(screen.getByText(/fail with 403/i)).toBeInTheDocument();
  });

  it('reports missing event subscriptions', () => {
    render(
      <GitHubTile
        {...tileProps()}
        platformVerification={{ ...allHealthy, app_events_match: false }}
      />,
    );

    expect(screen.getByText(/silently never fire/i)).toBeInTheDocument();
  });

  it('renders an unverifiable check as amber, not as drift', () => {
    // GitHub was unreachable. "Could not determine" must never read as "broken".
    render(
      <GitHubTile
        {...tileProps()}
        platformVerification={{
          ...allHealthy,
          app_webhook_url_matches: null,
          app_permissions_match: null,
          app_events_match: null,
        }}
      />,
    );

    expect(screen.getByText(/could not be verified/i)).toBeInTheDocument();
    expect(screen.queryByText(/not fully wired for GitHub/i)).not.toBeInTheDocument();
  });

  it('shows the backend detail for a failing check', () => {
    render(
      <GitHubTile
        {...tileProps()}
        platformVerification={{
          ...allHealthy,
          app_events_match: false,
          app_config_warnings: ['Missing event subscriptions: label. Enable in GitHub App Settings.'],
        }}
      />,
    );

    expect(screen.getByText(/Missing event subscriptions: label/)).toBeInTheDocument();
  });

  it('does not leak drift detail when the API omitted the platform block', () => {
    // Non-admins get no platform_verification. Absence is not health.
    render(<GitHubTile {...tileProps()} platformVerification={null} />);

    expect(screen.queryByText(/App webhook URL/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Expected OAuth callback URL/)).not.toBeInTheDocument();
  });
});

describe('Callback URL reporting (Issue #4017)', () => {
  const withCallback: PlatformVerification = {
    ...allHealthy,
    expected_callback_url: 'https://api.example.com/dev/auth/github/callback',
    app_oauth_settings_url: 'https://github.com/settings/apps/adp-agent-dev/oauth',
  };

  it('reports the expected callback URL for eyeball comparison', () => {
    render(<GitHubTile {...tileProps()} platformVerification={withCallback} />);

    expect(
      screen.getByText('https://api.example.com/dev/auth/github/callback'),
    ).toBeInTheDocument();
  });

  it('never renders the callback URL as a pass/fail check', () => {
    // The whole point of the approved design: it CANNOT be diffed, so it must
    // not appear as a verification row alongside the diffable checks.
    render(<GitHubTile {...tileProps()} platformVerification={withCallback} />);

    expect(screen.getByText(/does not expose an App's callback URL/i)).toBeInTheDocument();
    expect(screen.queryByText(/Callback URL mismatch/i)).not.toBeInTheDocument();
  });

  it('deep-links to the App OAuth settings page', () => {
    render(<GitHubTile {...tileProps()} platformVerification={withCallback} />);

    const link = screen.getByRole('link', { name: /OAuth settings on GitHub/i });
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/settings/apps/adp-agent-dev/oauth',
    );
  });

  it('omits the callback section when the URL could not be resolved', () => {
    render(<GitHubTile {...tileProps()} platformVerification={allHealthy} />);

    expect(screen.queryByText(/Expected OAuth callback URL/)).not.toBeInTheDocument();
  });
});

describe('Re-validate config action (Issue #4017)', () => {
  it('invokes the re-check handler when clicked', async () => {
    const onRevalidateApp = vi.fn().mockResolvedValue(undefined);
    render(
      <GitHubTile
        {...tileProps({ onRevalidateApp })}
        platformVerification={allHealthy}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Re-validate config/i }));

    expect(onRevalidateApp).toHaveBeenCalledTimes(1);
  });

  it('is hidden when the caller cannot manage the App', () => {
    render(<GitHubTile {...tileProps()} platformVerification={allHealthy} />);

    expect(
      screen.queryByRole('button', { name: /Re-validate config/i }),
    ).not.toBeInTheDocument();
  });

  it('leaves the destructive actions intact', () => {
    // The new button sits beside Rotate key / Disconnect app; it must not
    // displace them.
    render(
      <GitHubTile
        {...tileProps({ onRevalidateApp: vi.fn() })}
        platformVerification={allHealthy}
      />,
    );

    expect(screen.getByRole('button', { name: /Rotate key/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Disconnect app/i })).toBeInTheDocument();
  });
});
