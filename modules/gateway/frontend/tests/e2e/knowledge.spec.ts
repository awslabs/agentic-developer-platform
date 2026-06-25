/**
 * Playwright E2E smoke tests for the Knowledge management page.
 *
 * Issue #1794 (Story E of E10 #1736).
 *
 * These tests mock the backend API at the network level.
 * They exercise the three-zone layout, asset list, and add-asset dialog.
 *
 * Usage:
 *   npx playwright test tests/e2e/knowledge.spec.ts
 *
 * Requires:
 *   - Frontend running on localhost (or GATEWAY_URL env var)
 */

import { test, expect, type Page, type Route } from '@playwright/test';

const BASE_URL = process.env.GATEWAY_URL || 'http://localhost:5173';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function injectAuth(page: Page) {
  await page.addInitScript(() => {
    const expiry = Date.now() + 3600_000;
    const header = btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
    const payload = btoa(
      JSON.stringify({
        sub: 'test-user-id',
        email: 'test@example.com',
        'custom:role': 'platform_admin',
        'custom:org_id': 'org-001',
        exp: Math.floor(expiry / 1000),
      }),
    );
    const sig = btoa('fake-signature');
    const token = `${header}.${payload}.${sig}`;

    window.sessionStorage.setItem('cognito_access_token', token);
    window.sessionStorage.setItem('cognito_id_token', token);
    window.sessionStorage.setItem('cognito_refresh_token', 'fake-refresh');
    window.sessionStorage.setItem('cognito_token_expiry', String(expiry));
  });
}

const sampleAssets = [
  {
    id: 'asset-001',
    asset_type: 'repo',
    source_ref: 'https://github.com/acme/service-a',
    display_name: 'acme/service-a',
    tags: {},
    metadata: {},
    tenant_id: 'org-001',
    owner_sub: null,
    project_id: null,
    status: 'indexed',
    last_error: null,
    retry_count: 0,
    registered_by: 'test-user-id',
    created_at: '2026-06-20T10:00:00Z',
    updated_at: '2026-06-20T11:00:00Z',
  },
  {
    id: 'asset-002',
    asset_type: 'url',
    source_ref: 'https://docs.example.com/api',
    display_name: 'API Reference',
    tags: {},
    metadata: {},
    tenant_id: 'org-001',
    owner_sub: 'test-user-id',
    project_id: null,
    status: 'failed',
    last_error: 'Crawl timeout',
    retry_count: 2,
    registered_by: 'test-user-id',
    created_at: '2026-06-19T08:00:00Z',
    updated_at: '2026-06-19T09:00:00Z',
  },
];

async function mockAssetsApi(page: Page) {
  // List endpoint
  await page.route('**/api/agent-context/assets?*', (route: Route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: sampleAssets,
          total: 2,
          page: 1,
          page_size: 20,
          has_more: false,
          quota: {
            repos: { used: 1, limit: 20 },
            urls: { used: 1, limit: 50 },
            docs: { used: 0, limit: 20 },
          },
        }),
      });
    }
    return route.continue();
  });

  // Also match list without query params
  await page.route('**/api/agent-context/assets', (route: Route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: sampleAssets,
          total: 2,
          page: 1,
          page_size: 20,
          has_more: false,
          quota: {
            repos: { used: 1, limit: 20 },
            urls: { used: 1, limit: 50 },
            docs: { used: 0, limit: 20 },
          },
        }),
      });
    }
    if (route.request().method() === 'POST') {
      // Create asset
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 'asset-new',
          asset_type: 'url',
          source_ref: 'https://example.com/new',
          display_name: 'New Asset',
          tags: {},
          metadata: {},
          tenant_id: 'org-001',
          owner_sub: null,
          project_id: null,
          status: 'registered',
          last_error: null,
          retry_count: 0,
          registered_by: 'test-user-id',
          created_at: '2026-06-25T12:00:00Z',
          updated_at: null,
        }),
      });
    }
    return route.continue();
  });

  // Detail endpoint
  await page.route('**/api/agent-context/assets/asset-001', (route: Route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(sampleAssets[0]),
      });
    }
    return route.continue();
  });

  await page.route('**/api/agent-context/assets/asset-002', (route: Route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(sampleAssets[1]),
      });
    }
    return route.continue();
  });

  // Repo picker
  await page.route('**/api/agent-context/github/accessible-repos*', (route: Route) => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        repos: [
          { full_name: 'acme/service-a', private: true, url: 'https://github.com/acme/service-a' },
          { full_name: 'acme/docs', private: false, url: 'https://github.com/acme/docs' },
        ],
        total: 2,
        page: 1,
        has_more: false,
      }),
    });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Knowledge page', () => {
  test('renders page header and three-zone layout', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // Page title
    await expect(page.locator('h1')).toContainText('Knowledge');

    // Asset list shows items
    await expect(page.getByText('acme/service-a')).toBeVisible();
    await expect(page.getByText('API Reference')).toBeVisible();

    // Status badges
    await expect(page.getByText('indexed')).toBeVisible();
    await expect(page.getByText('failed')).toBeVisible();

    // Right zone stub
    await expect(page.getByText('Project Context')).toBeVisible();
  });

  test('shows asset detail when item is clicked', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    await expect(page.getByText('acme/service-a')).toBeVisible();

    // Click asset
    await page.getByText('acme/service-a').click();

    // Detail should show metadata
    await expect(page.getByText('Repo')).toBeVisible();
    await expect(page.getByText('Reindex')).toBeVisible();
    await expect(page.getByText('Remove')).toBeVisible();
  });

  test('add asset dialog opens and shows repo picker', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // Click Add Asset button
    await page.getByText('Add Asset').click();

    // Dialog should be visible
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByText('Add Asset', { exact: false })).toBeVisible();

    // Repo tab active by default — should show repos
    await expect(page.getByText('acme/service-a')).toBeVisible();
    await expect(page.getByText('acme/docs')).toBeVisible();
  });

  test('add asset via URL tab', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // Open dialog
    await page.getByText('Add Asset').click();
    await expect(page.getByRole('dialog')).toBeVisible();

    // Switch to URL tab
    await page.getByRole('tab', { name: 'URL' }).click();

    // Fill URL input
    await page.getByPlaceholder('https://example.com/docs/page').fill('https://example.com/new');

    // Submit
    const addButtons = page.getByRole('button', { name: /Add Asset/ });
    await addButtons.last().click();

    // Dialog should close (submit successful)
    await expect(page.getByRole('dialog')).not.toBeVisible();
  });

  test('shows quota info in left rail', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    await expect(page.getByText('Repos: 1/20')).toBeVisible();
    await expect(page.getByText('URLs: 1/50')).toBeVisible();
  });

  test('scope tabs filter the list', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // "All" tab is active by default
    await expect(page.getByRole('tab', { name: 'All' })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    // Click "Personal" tab
    await page.getByRole('tab', { name: 'Personal' }).click();

    // Verify the tab became active
    await expect(page.getByRole('tab', { name: 'Personal' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });
});
