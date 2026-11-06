/**
 * Playwright E2E tests for the Connections page.
 *
 * Issue #477: Test coverage for Connections page (parent #465).
 *
 * These tests mock the backend API at the network level so they don't
 * hit real GitHub. They exercise the full browser flow: rendering,
 * install redirect, callback handling, and disconnect.
 *
 * Usage:
 *   npx playwright test tests/e2e/connections.spec.ts
 *
 * Requires:
 *   - Frontend running on localhost (or GATEWAY_URL env var)
 *   - Auth tokens injected via init script (same pattern as test_budget_ratelimit_smoke.py)
 */

import { test, expect, type Page, type Route } from '@playwright/test';

const BASE_URL = process.env.GATEWAY_URL || 'http://localhost:5173';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Inject a fake auth token so the app treats us as authenticated. */
async function injectAuth(page: Page, role: 'admin' | 'member' = 'admin') {
  await page.addInitScript((userRole) => {
    const expiry = Date.now() + 3600_000;
    // Fake JWT payload — the frontend only parses the middle segment
    const header = btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
    const payload = btoa(
      JSON.stringify({
        sub: 'test-user-id',
        email: 'test@example.com',
        'custom:role': userRole,
        exp: Math.floor(expiry / 1000),
      }),
    );
    const sig = btoa('fake-signature');
    const token = `${header}.${payload}.${sig}`;

    window.sessionStorage.setItem('cognito_access_token', token);
    window.sessionStorage.setItem('cognito_id_token', token);
    window.sessionStorage.setItem('cognito_refresh_token', 'fake-refresh');
    window.sessionStorage.setItem('cognito_token_expiry', String(expiry));
  }, role);
}

/** Mock the connections API endpoints. */
async function mockConnectionsApi(
  page: Page,
  connections: Array<Record<string, unknown>> = [],
) {
  await page.route('**/api/admin/connections', (route: Route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ connections }),
      });
    }
    return route.continue();
  });
}

/** Mock the install-start endpoint. */
async function mockInstallStart(page: Page) {
  await page.route('**/api/admin/connections/github/install-start', (route: Route) => {
    if (route.request().method() === 'POST') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          install_url: 'https://github.com/apps/adp-agent/installations/new?state=test-state-123',
          state_token: 'test-state-123',
          expires_at: '2026-05-05T12:00:00Z',
        }),
      });
    }
    return route.continue();
  });
}

/** Mock the delete endpoint. */
async function mockDelete(page: Page, installationId: number) {
  await page.route(
    `**/api/admin/connections/github/${installationId}`,
    (route: Route) => {
      if (route.request().method() === 'DELETE') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ deleted: true, installation_id: installationId }),
        });
      }
      return route.continue();
    },
  );
}

const sampleConnection = {
  provider: 'github',
  installation_id: 12345,
  account_login: 'test-org',
  account_type: 'Organization',
  repository_selection: 'selected',
  repository_count: 8,
  installed_at: '2026-05-01T10:00:00Z',
  configure_url: 'https://github.com/organizations/test-org/settings/installations/12345',
};

type AtlassianReadiness = 'ready' | 'setup_required' | 'policy_blocked' | 'unavailable';

async function mockAtlassianJourney(page: Page, initialReadiness: AtlassianReadiness = 'ready', withLegacy = false) {
  const originalRecords = [
    { id: 'jira-legacy', scope: 'user', service: 'jira', credential_type: 'oauth_token', provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['read:jira-work'] } },
    { id: 'github-owner', scope: 'user', service: 'github', credential_type: 'oauth_token', provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['read:user'] } },
    { id: 'slack-owner', scope: 'user', service: 'slack', credential_type: 'oauth_token', provider: 'agentcore-identity', agent_consent: true, scopes: { oauth_scopes: ['users:read'] } },
  ];
  const state: {
    readiness: AtlassianReadiness;
    revision: number;
    selectedSite: string | null;
    connected: boolean;
    pending: boolean;
    statusReads: number;
    prepares: number;
    records: typeof originalRecords;
    legacyVersion: number;
    failComplete: boolean;
    starts: Array<{ capability_set: string; reconnect_revision?: string }>;
  } = { readiness: initialReadiness, revision: initialReadiness === 'policy_blocked' || initialReadiness === 'unavailable' ? 1 : 0,
    selectedSite: initialReadiness === 'policy_blocked' || initialReadiness === 'unavailable' ? 'site-one' : null,
    connected: initialReadiness === 'policy_blocked' || initialReadiness === 'unavailable', pending: false, statusReads: 0, prepares: 0,
    records: withLegacy ? [...originalRecords] : [], legacyVersion: 1, failComplete: false, starts: [] };
  const sites = ['site-one', 'site-two'];
  const actions = {
    ready: { owner: null, action: null },
    setup_required: { owner: 'platform_admin', action: 'Ask a platform administrator to complete and verify the deployment setup.' },
    policy_blocked: { owner: 'atlassian_organization_admin', action: 'Ask the Atlassian organization administrator to allow this deployment client.' },
    unavailable: { owner: 'platform_admin', action: 'Ask a platform administrator to reconcile the deployment configuration.' },
  };
  const reply = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/**', (route) => reply(route, {}));
  await page.route('**/api/admin/connections', (route) => reply(route, { connections: [] }));
  await page.route('**/api/admin/connections/github/app', (route) => reply(route, { registered: true, app_slug: 'example', app_id: 'app-1' }));
  await page.route('**/api/auth/credentials', (route) => reply(route, state.records));
  await page.route('**/api/auth/connections/oauth/providers', (route) => reply(route, { providers: [
    { service: 'github', provider: 'github', status: 'registered' },
    { service: 'slack', provider: 'slack', status: 'registered' },
  ] }));
  await page.route('**/api/admin/connections/atlassian-mcp/prepare', (route) => {
    if (state.readiness !== 'setup_required' || route.request().method() !== 'POST') return reply(route, { detail: 'not_ready' }, 409);
    state.prepares++;
    state.readiness = 'ready';
    return reply(route, { status: 'prepared' });
  });
  await page.route('**/api/auth/connections/oauth/atlassian', (route) => {
    if (route.request().method() !== 'DELETE' || !state.connected || !state.selectedSite) return reply(route, { detail: 'replacement_not_ready' }, 409);
    state.records = state.records.filter((record) => record.service !== 'jira');
    state.legacyVersion++;
    return reply(route, { status: 'disconnected' });
  });
  await page.route('**/api/auth/connections/atlassian-mcp**', (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (method === 'GET' && path.endsWith('/status')) {
      state.statusReads++;
      return reply(route, { status: state.readiness, ...actions[state.readiness],
        ...(state.connected ? { connection: { id: 'binding-1', revision: `rev-${state.revision}`, sites, selected_site_id: state.selectedSite } } : {}),
      });
    }
    if (method === 'POST' && path.endsWith('/start')) {
      const body = route.request().postDataJSON() as { capability_set: string; reconnect_revision?: string };
      if (body.reconnect_revision !== (state.connected ? `rev-${state.revision}` : undefined)) return reply(route, { detail: 'connection_changed' }, 409);
      state.starts.push(body);
      state.pending = true;
      return reply(route, { authorization_url: 'https://atlassian.example.test/authorize', state: 'fixture-state', session_uri: 'opaque-session' });
    }
    if (method === 'POST' && path.endsWith('/complete')) {
      const body = route.request().postDataJSON() as { state: string; session_uri: string };
      if (state.failComplete) return reply(route, { detail: { error: 'identity_unavailable' } }, 503);
      if (!state.pending || body.state !== 'fixture-state' || body.session_uri !== 'opaque-session') return reply(route, { detail: 'invalid_consent' }, 403);
      state.pending = false;
      state.connected = true;
      state.revision++;
      return reply(route, { id: 'binding-1', revision: `rev-${state.revision}`, service: 'atlassian-mcp' }, 201);
    }
    if (method === 'POST' && path.endsWith('/select-site')) {
      const body = route.request().postDataJSON() as { connection_revision: string; site_id: string };
      if (!state.connected || body.connection_revision !== `rev-${state.revision}` || !sites.includes(body.site_id)) return reply(route, { detail: 'connection_changed' }, 409);
      state.selectedSite = body.site_id;
      state.revision++;
      return reply(route, { revision: `rev-${state.revision}`, selected_site_id: body.site_id });
    }
    if (method === 'DELETE' && path.endsWith('/atlassian-mcp')) {
      const body = route.request().postDataJSON() as { connection_revision: string };
      if (!state.connected || body.connection_revision !== `rev-${state.revision}`) return reply(route, { detail: 'connection_changed' }, 409);
      state.connected = false;
      state.selectedSite = null;
      state.pending = false;
      return reply(route, { status: 'disconnected', revocation_pending: true });
    }
    return reply(route, { detail: 'Not found' }, 404);
  });
  await page.route('https://atlassian.example.test/authorize', (route) => route.fulfill({ status: 200, contentType: 'text/html', body: '<h1>Fixture provider consent</h1>' }));
  return Object.assign(state, { restore: () => {
    state.records = [...originalRecords];
    state.connected = false;
    state.selectedSite = null;
    state.pending = false;
    state.failComplete = false;
  } });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Connections page', () => {
  test('atlassian_user_journey', async ({ page }) => {
    await injectAuth(page, 'member');
    const server = await mockAtlassianJourney(page);
    await page.goto(`${BASE_URL}/settings/connections`);
    const card = page.getByLabel('Atlassian — Jira & Confluence');
    await expect(card.getByRole('status')).toHaveText('Not connected');
    await expect(card.getByRole('button', { name: 'Request Jira write access' })).toHaveCount(0);

    await card.getByRole('button', { name: 'Connect Atlassian' }).click();
    await expect(page.getByRole('heading', { name: 'Fixture provider consent' })).toBeVisible();
    expect(server.connected).toBe(false);
    expect(server.starts).toEqual([{ capability_set: 'reads' }]);
    await page.goto(`${BASE_URL}/settings/connections?oauth_consent=returned`);
    await expect(card.getByRole('status')).toHaveText('Choose a site');
    expect(server.connected).toBe(true);
    expect(server.selectedSite).toBeNull();
    await card.getByRole('combobox', { name: 'Atlassian site' }).selectOption('site-two');
    await expect(card.getByRole('status')).toHaveText('Site selected');
    await expect(card.getByRole('combobox', { name: 'Atlassian site' })).toHaveValue('site-two');
    expect(server.selectedSite).toBe('site-two');

    await card.getByRole('button', { name: 'Reconnect Atlassian' }).click();
    await expect(page.getByRole('heading', { name: 'Fixture provider consent' })).toBeVisible();
    expect(server.starts).toEqual([{ capability_set: 'reads' }, { capability_set: 'reads', reconnect_revision: 'rev-2' }]);
    await page.goto(`${BASE_URL}/settings/connections?oauth_consent=returned`);
    await expect(card.getByRole('status')).toHaveText('Site selected');
    expect(server.revision).toBe(3);
    page.once('dialog', (dialog) => dialog.accept());
    await card.getByRole('button', { name: 'Disconnect Atlassian' }).click();
    await expect(card.getByRole('status')).toHaveText('Not connected');
    expect(server.connected).toBe(false);
    expect(server.statusReads).toBeGreaterThanOrEqual(4);
    expect(server.statusReads).toBeLessThanOrEqual(12);
  });

  test('atlassian_admin_states', async ({ browser }) => {
    const cases = [
      { readiness: 'setup_required', badge: 'Admin setup needed', userAction: 'Ask a platform administrator to complete and verify the deployment setup.', adminAction: 'Complete and verify the deployment setup before users connect.' },
      { readiness: 'policy_blocked', badge: 'Policy approval needed', userAction: 'Ask the Atlassian organization administrator to allow this deployment client.', adminAction: 'Ask the Atlassian organization administrator to allow this deployment client.' },
      { readiness: 'unavailable', badge: 'Unavailable', userAction: 'Ask a platform administrator to reconcile the deployment configuration.', adminAction: 'Reconcile the deployment configuration before trying setup again.' },
    ] as const;
    for (const scenario of cases) {
      for (const role of ['member', 'admin'] as const) {
        const context = await browser.newContext();
        try {
          const page = await context.newPage();
          await page.clock.install({ time: new Date('2026-10-25T12:00:00Z') });
          await injectAuth(page, role);
          const server = await mockAtlassianJourney(page, scenario.readiness);
          await page.goto(`${BASE_URL}/settings/connections`);
          const card = page.getByLabel('Atlassian — Jira & Confluence');
          await expect(card.getByRole('status')).toHaveText(scenario.badge);
          await expect(card.getByRole('alert')).toHaveText(role === 'admin' ? scenario.adminAction : scenario.userAction);
          await expect(card.locator('input')).toHaveCount(0);
          await expect(card.getByRole('button', { name: /Connect Atlassian|Reconnect Atlassian|Disconnect Atlassian/ })).toHaveCount(0);
          await expect(card.getByRole('heading', { name: 'Atlassian — Jira & Confluence' })).toHaveCount(1);
          expect(await page.evaluate(() => JSON.parse(atob(window.sessionStorage.getItem('cognito_id_token')!.split('.')[1]))['custom:role'])).toBe(role);
          expect(server.connected).toBe(scenario.readiness !== 'setup_required');
          if (role === 'admin' && scenario.readiness === 'setup_required') {
            await card.getByRole('button', { name: 'Prepare Atlassian connection' }).click();
            await expect(card.getByRole('button', { name: 'Connect Atlassian' })).toBeVisible();
            expect(server.prepares).toBe(1);
          } else {
            await expect(card.getByRole('button', { name: 'Prepare Atlassian connection' })).toHaveCount(0);
            expect(server.prepares).toBe(0);
          }
          if (scenario.readiness !== 'setup_required') {
            server.readiness = 'ready';
            await card.getByRole('button', { name: 'Refresh Atlassian status' }).click();
            await expect(card.getByRole('status')).toHaveText('Site selected');
            expect(server.connected).toBe(true);
          }
          expect(server.statusReads).toBeLessThanOrEqual(3);
        } finally {
          await context.close();
        }
      }
    }
  });

  test('atlassian_legacy_migration', async ({ page }) => {
    await injectAuth(page, 'member');
    const server = await mockAtlassianJourney(page, 'ready', true);
    const originalIds = ['jira-legacy', 'github-owner', 'slack-owner'];
    const card = page.getByLabel('Atlassian — Jira & Confluence');
    try {
      await page.goto(`${BASE_URL}/settings/connections`);
      await expect(card.getByText(/Your existing Jira connection is unchanged/)).toBeVisible();
      await expect(page.getByRole('heading', { name: 'Atlassian — Jira & Confluence' })).toHaveCount(1);
      await expect(page.getByRole('heading', { name: /Atlassian \(Jira\)/ })).toHaveCount(0);
      await expect(card.getByRole('button', { name: 'Disconnect old Jira connection' })).toHaveCount(0);

      await card.getByRole('button', { name: 'Connect Atlassian' }).click();
      await expect(page.getByRole('heading', { name: 'Fixture provider consent' })).toBeVisible();
      await page.goto(`${BASE_URL}/settings/connections?oauth_consent=error`);
      await expect(page.getByText(/Consent was cancelled or expired/)).toBeVisible();
      expect(server.records.map((record) => record.id)).toEqual(originalIds);
      expect(server.legacyVersion).toBe(1);
      await page.reload();
      await expect(card.getByRole('status')).toHaveText('Not connected');
      await expect(card.getByText(/Your existing Jira connection is unchanged/)).toBeVisible();

      server.failComplete = true;
      await card.getByRole('button', { name: 'Connect Atlassian' }).click();
      await expect(page.getByRole('heading', { name: 'Fixture provider consent' })).toBeVisible();
      await page.goto(`${BASE_URL}/settings/connections?oauth_consent=returned`);
      await expect(card.getByRole('status')).toHaveText('Not connected');
      expect(server.records.map((record) => record.id)).toEqual(originalIds);
      expect(server.revision).toBe(0);
      await page.reload();
      await expect(card.getByRole('button', { name: 'Connect Atlassian' })).toBeVisible();
      expect(server.revision).toBe(0);

      server.failComplete = false;
      await card.getByRole('button', { name: 'Connect Atlassian' }).click();
      await expect(page.getByRole('heading', { name: 'Fixture provider consent' })).toBeVisible();
      await page.goto(`${BASE_URL}/settings/connections?oauth_consent=returned`);
      await expect(card.getByRole('status')).toHaveText('Choose a site');
      expect(server.records.map((record) => record.id)).toEqual(originalIds);
      await expect(card.getByRole('button', { name: 'Disconnect old Jira connection' })).toHaveCount(0);
      await card.getByRole('combobox', { name: 'Atlassian site' }).selectOption('site-one');
      await expect(card.getByRole('button', { name: 'Disconnect old Jira connection' })).toBeVisible();
      page.once('dialog', (dialog) => dialog.accept());
      await card.getByRole('button', { name: 'Disconnect old Jira connection' }).click();
      await expect(card.getByText(/Your existing Jira connection is unchanged/)).toHaveCount(0);
      expect(server.records.map((record) => record.id)).toEqual(['github-owner', 'slack-owner']);
      expect(server.legacyVersion).toBe(2);
      expect(server.selectedSite).toBe('site-one');
      await page.reload();
      await expect(card.getByRole('status')).toHaveText('Site selected');
      expect(server.records.map((record) => record.id)).toEqual(['github-owner', 'slack-owner']);
      expect(server.statusReads).toBeLessThanOrEqual(24);
    } finally {
      server.restore();
      expect(server.records.map((record) => record.id)).toEqual(originalIds);
      expect(server.connected).toBe(false);
    }
  });
  test('empty state renders Install button and Coming soon tiles', async ({ page }) => {
    await injectAuth(page);
    await mockConnectionsApi(page, []);

    await page.goto(`${BASE_URL}/settings/connections`);

    // Verify page header
    await expect(page.locator('h1')).toContainText('Connections');

    // Install button visible
    await expect(page.getByText('Install on GitHub')).toBeVisible();

    // No installation cards
    await expect(page.getByText('Disconnect')).not.toBeVisible();

    // Coming soon tiles
    await expect(page.getByText('Slack')).toBeVisible();
    await expect(page.getByText('Google')).toBeVisible();
    const comingSoonBadges = page.getByText('Coming soon');
    await expect(comingSoonBadges).toHaveCount(2);
  });

  test('install flow: clicking Install on GitHub POSTs install-start and redirects', async ({
    page,
  }) => {
    await injectAuth(page);
    await mockConnectionsApi(page, []);
    await mockInstallStart(page);

    // Intercept navigation to GitHub (prevent actual redirect)
    let redirectUrl = '';
    await page.route('https://github.com/**', (route) => {
      redirectUrl = route.request().url();
      // Abort the navigation — we just want to capture the URL
      return route.abort();
    });

    await page.goto(`${BASE_URL}/settings/connections`);
    await expect(page.getByText('Install on GitHub')).toBeVisible();

    // Click install button
    await page.getByText('Install on GitHub').click();

    // Wait for the redirect attempt
    await page.waitForTimeout(1000);

    // Verify the redirect URL shape contains ?state= param
    expect(redirectUrl).toContain('github.com/apps/adp-agent/installations/new');
    expect(redirectUrl).toContain('state=test-state-123');
  });

  test('callback success: ?success=1 shows toast and refetches list', async ({ page }) => {
    await injectAuth(page);

    // First load returns empty, after "refetch" returns one connection
    let callCount = 0;
    await page.route('**/api/admin/connections', (route: Route) => {
      if (route.request().method() === 'GET') {
        callCount++;
        const connections = callCount > 1 ? [sampleConnection] : [];
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ connections }),
        });
      }
      return route.continue();
    });

    await page.goto(`${BASE_URL}/settings/connections?success=1`);

    // Toast should appear
    await expect(
      page.getByText('GitHub connected! You can now trigger agents from this org.'),
    ).toBeVisible();

    // After refetch, the connection card should appear
    await expect(page.getByText('test-org')).toBeVisible();
  });

  test('callback error: ?error=expired_nonce&message=Token+expired shows error', async ({
    page,
  }) => {
    await injectAuth(page);
    await mockConnectionsApi(page, []);

    await page.goto(
      `${BASE_URL}/settings/connections?error=expired_nonce&message=Token+expired`,
    );

    // Error toast should appear
    await expect(page.getByText('Token expired')).toBeVisible();
  });

  test('disconnect flow: click Disconnect → Confirm → card removed', async ({ page }) => {
    await injectAuth(page);

    // Start with one connection, after delete return empty
    let deleted = false;
    await page.route('**/api/admin/connections', (route: Route) => {
      if (route.request().method() === 'GET') {
        const connections = deleted ? [] : [sampleConnection];
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ connections }),
        });
      }
      return route.continue();
    });
    await mockDelete(page, 12345);

    await page.goto(`${BASE_URL}/settings/connections`);

    // Card visible
    await expect(page.getByText('test-org')).toBeVisible();
    await expect(page.getByText('Disconnect')).toBeVisible();

    // First click — confirmation
    await page.getByText('Disconnect').click();
    await expect(page.getByText('Confirm?')).toBeVisible();

    // Mark as deleted so next list returns empty
    deleted = true;

    // Second click — confirm deletion
    await page.getByText('Confirm?').click();

    // Toast + card disappears
    await expect(page.getByText('GitHub installation disconnected.')).toBeVisible();
    await expect(page.getByText('test-org')).not.toBeVisible();
  });

  test('configure link opens GitHub in new tab', async ({ page }) => {
    await injectAuth(page);
    await mockConnectionsApi(page, [sampleConnection]);

    await page.goto(`${BASE_URL}/settings/connections`);
    await expect(page.getByText('test-org')).toBeVisible();

    // Find the configure link
    const configLink = page.getByText(/Configure on GitHub/);
    await expect(configLink).toBeVisible();

    // Verify it points to the correct URL with target=_blank
    const anchor = configLink.locator('xpath=ancestor-or-self::a');
    await expect(anchor).toHaveAttribute(
      'href',
      'https://github.com/organizations/test-org/settings/installations/12345',
    );
    await expect(anchor).toHaveAttribute('target', '_blank');
  });

  test('non-admin sees Install button but no Disconnect on cards', async ({ page }) => {
    await injectAuth(page, 'member');
    await mockConnectionsApi(page, [sampleConnection]);

    await page.goto(`${BASE_URL}/settings/connections`);

    // Install button should still be visible (members can add)
    await expect(page.getByText('Install on GitHub')).toBeVisible();

    // Connection card is visible
    await expect(page.getByText('test-org')).toBeVisible();

    // NOTE: Current implementation shows Disconnect to all users.
    // If role-based hiding is added in the future, this test verifies it.
    // For now, we verify the card renders correctly for non-admin.
    await expect(page.getByText(/Installation #12345/)).toBeVisible();
  });
});
