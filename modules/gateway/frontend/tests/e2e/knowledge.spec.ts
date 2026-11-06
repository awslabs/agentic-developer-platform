/**
 * Playwright E2E smoke tests for the Context page.
 *
 * Issue #1794 (Story E of E10 #1736).
 *
 * These tests mock the backend API at the network level.
 * They exercise the responsive list/detail layout, indexing status, and dialogs.
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
        auth_time: Math.floor(Date.now() / 1000),
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
    owner_sub: 'test-user-id',
    project_id: null,
    product_id: null,
    can_manage: true,
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
    product_id: null,
    can_manage: true,
    status: 'failed',
    last_error: 'Crawl timeout',
    retry_count: 2,
    registered_by: 'test-user-id',
    created_at: '2026-06-19T08:00:00Z',
    updated_at: '2026-06-19T09:00:00Z',
  },
];

async function mockAssetsApi(page: Page) {
  // Browser tests only use local fixture APIs; never forward an unmatched call.
  await page.route('**/api/**', (route) => route.fulfill({ status: 404, json: { detail: 'Fixture route not configured' } }));
  await page.route('**/api/access/status', (route) => route.fulfill({ json: { status: 'registered' } }));
  await page.route('**/api/auth/workspaces', (route) => route.fulfill({ json: { items: [] } }));
  await page.route('**/api/auth/workflow-permissions', (route) => route.fulfill({
    json: { org_id: 'org-001', permissions: [] },
  }));
  await page.route('**/api/features', (route) => route.fulfill({ json: { features: {
    knowledge: true, indexing: true, chat: true, connections: true, credentials: true,
    system_dashboard: true, logs: true, new_ui: false,
  } } }));

  await page.route('**/api/agent-context/assets/*/status', (route) => route.fulfill({
    json: {
      asset_id: 'asset-001', source_ref: sampleAssets[0].source_ref, repo_found: true,
      run_id: 'run-001', run_status: 'completed', run_started_at: '2026-06-20T10:00:00Z',
      stages: [{ stage: 'clone', status: 'verified', metrics: { files: 150 }, error: null }],
    },
  }));

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
          owner_sub: 'test-user-id',
          project_id: null,
          product_id: null,
          can_manage: true,
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

test.describe('Context page', () => {
  test('renders the asset list without the unavailable project panel', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // Page title
    await expect(page.getByRole('heading', { name: 'Context', exact: true })).toBeVisible();

    // Asset list shows items
    await expect(page.getByText('acme/service-a', { exact: true })).toBeVisible();
    await expect(page.getByText('API Reference')).toBeVisible();

    // Status badges
    await expect(page.getByText('indexed', { exact: true })).toBeVisible();
    await expect(page.getByText('failed')).toBeVisible();

    await expect(page.getByText('Project Context')).toHaveCount(0);
  });

  test('shows asset detail when item is clicked', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    await expect(page.getByText('acme/service-a', { exact: true })).toBeVisible();

    // Click asset
    await page.getByRole('button', { name: /acme\/service-a/ }).click();

    // Detail should show metadata
    await expect(page.getByText('Added by', { exact: true })).toBeVisible();
    await expect(page.getByText('Reindex')).toBeVisible();
    await expect(page.getByText('Remove')).toBeVisible();
    await expect(page.getByTestId('stage-chip-clone')).toContainText('verified:');
    await expect(page.getByTestId('stage-metrics-clone')).toContainText('150 files');
    await expect(page.getByText('Not yet indexed')).toHaveCount(0);
  });

  test('Add information opens with an optional connected repository picker', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // Click Add information button
    await page.getByText('Add information').click();

    // Dialog should be visible
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('dialog').getByRole('heading', { name: 'Add information' })).toBeVisible();

    await expect(page.getByRole('dialog').getByRole('tab')).toHaveCount(0);
    await page.getByRole('button', { name: 'Choose a connected repository' }).click();
    // The picker uses the existing connection.
    await expect(page.getByRole('dialog').getByText('acme/service-a')).toBeVisible();
    await expect(page.getByRole('dialog').getByText('acme/docs')).toBeVisible();
  });

  test('adds a web link to personal Context', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);

    await page.goto(`${BASE_URL}/knowledge`);

    // Open dialog
    await page.getByText('Add information').click();
    await expect(page.getByRole('dialog')).toBeVisible();

    // Fill URL input
    await page.getByLabel('Link', { exact: true }).fill('https://example.com/new');

    // Submit
    const addButtons = page.getByRole('button', { name: /Add information/ });
    await addButtons.last().click();

    // Dialog should close (submit successful)
    await expect(page.getByRole('dialog')).not.toBeVisible();
  });

  test('registers an authorized S3 document from its link', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);
    await page.goto(`${BASE_URL}/knowledge`);
    await page.getByRole('button', { name: 'Add information', exact: true }).click();
    await page.getByLabel('Link', { exact: true }).fill('s3://example-documents/manual.pdf');
    await page.getByLabel('Name (optional)').fill('Technical manual');
    const submission = page.waitForRequest((request) =>
      request.method() === 'POST' && request.url().endsWith('/agent-context/assets'));
    await page.getByRole('dialog').getByRole('button', { name: 'Add information', exact: true }).click();
    expect((await submission).postDataJSON()).toMatchObject({
      asset_type: 'doc', source_ref: 's3://example-documents/manual.pdf', display_name: 'Technical manual', scope: 'personal',
    });
    await expect(page.getByRole('dialog')).toBeHidden();
  });

  test('searches indexed contents and opens its cited source', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);
    await page.goto(`${BASE_URL}/knowledge`);
    await expect(page.getByText('2 items')).toBeVisible();
    await page.route('**/api/agent-context/assets/search', route => route.fulfill({ json: {
      query: 'How are releases deployed?',
      items: [{ asset: { ...sampleAssets[1], status: 'indexed' }, excerpt: 'Changes are released gradually through canary deployments.', heading: 'Release process' }],
    } }));
    const request = page.waitForRequest(request => request.url().endsWith('/agent-context/assets/search'));
    await page.getByRole('searchbox', { name: 'Search context' }).fill('How are releases deployed?');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    const submitted = await request;
    expect(new URL(submitted.url()).pathname).toBe('/api/agent-context/assets/search');
    expect(submitted.postDataJSON()).toEqual({ scope: 'personal', query: 'How are releases deployed?', limit: 20 });
    await expect(page.getByText('Changes are released gradually through canary deployments.')).toBeVisible();
    await page.getByRole('button', { name: /API Reference.*canary deployments/s }).click();
    await expect(page.getByRole('heading', { name: 'API Reference' })).toBeVisible();
  });

  test('opens personal context without sharing or category tabs', async ({ page }) => {
    await injectAuth(page);
    await mockAssetsApi(page);
    const list = page.waitForRequest(request => request.url().includes('/agent-context/assets?'));
    await page.goto(`${BASE_URL}/knowledge`);
    expect(new URL((await list).url()).searchParams.get('scope')).toBe('personal');
    await expect(page.getByText('Your personal information, available only to you.')).toBeVisible();
    await expect(page.getByRole('tab')).toHaveCount(0);
  });

  test('desktop cards stay compact instead of stretching to the sidebar height', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await injectAuth(page);
    await mockAssetsApi(page);
    await page.goto(`${BASE_URL}/knowledge`);
    await expect(page.getByText('2 items')).toBeVisible();

    const assets = page.getByRole('region', { name: 'Information', exact: true });
    const details = page.getByRole('region', { name: 'Information details' });
    const listBox = (await assets.boundingBox())!;
    const detailBox = (await details.boundingBox())!;
    expect(listBox.height).toBeLessThan(500);
    expect(detailBox.height).toBeLessThan(500);
    expect(detailBox.x).toBeGreaterThan(listBox.x + listBox.width);
    expect(Math.abs(detailBox.y - listBox.y)).toBeLessThan(2);
    await expect(assets.locator(':scope > div')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  });

  test('a long asset list scrolls inside its card', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await injectAuth(page);
    await mockAssetsApi(page);
    const items = Array.from({ length: 20 }, (_, i) => ({
      ...sampleAssets[0], id: `asset-${i}`, display_name: `service-${i}`,
    }));
    await page.route('**/api/agent-context/assets?*', (route) => route.fulfill({
      json: { items, total: 20, page: 1, page_size: 20, has_more: false, quota: null },
    }));
    await page.goto(`${BASE_URL}/knowledge`);
    await expect(page.getByText('20 items')).toBeVisible();
    const assets = page.getByRole('region', { name: 'Information', exact: true });
    expect((await assets.boundingBox())!.height).toBeLessThan(800);
    const scrollsInsideCard = await assets.getByRole('button', { name: /^service-0 / }).evaluate((button) => {
      const list = button.parentElement!;
      return list.scrollHeight > list.clientHeight && getComputedStyle(list).overflowY === 'auto';
    });
    expect(scrollsInsideCard).toBe(true);
    await assets.getByRole('button', { name: /^service-19 / }).scrollIntoViewIfNeeded();
    await expect(assets.getByRole('button', { name: /^service-19 / })).toBeInViewport();
  });

  for (const width of [390, 768, 1024]) {
    test(`list and details fit at ${width}px with keyboard focus restored on return`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await injectAuth(page);
      await mockAssetsApi(page);
      await page.route('**/api/agent-context/assets/asset-001', (route) => route.fulfill({ json: {
        ...sampleAssets[0], source_ref: `https://example.com/${'long-path-segment-'.repeat(20)}`,
      } }));
      await page.goto(`${BASE_URL}/knowledge`);
      const assets = page.getByRole('region', { name: 'Information', exact: true });
      const details = page.getByRole('region', { name: 'Information details' });
      await expect(assets).toBeVisible();
      await expect(details).toBeHidden();
      const listBox = (await assets.boundingBox())!;
      expect(listBox.x).toBeGreaterThanOrEqual(0);
      expect(listBox.x + listBox.width).toBeLessThanOrEqual(width);

      await assets.getByRole('button', { name: /acme\/service-a/ }).click();
      await expect(details.getByRole('heading', { name: 'acme/service-a' })).toBeFocused();
      await expect(assets).toBeHidden();
      await expect(page.getByTestId('stage-chip-clone')).toBeVisible();
      expect(await details.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      const detailBox = (await details.boundingBox())!;
      expect(detailBox.x).toBeGreaterThanOrEqual(0);
      expect(detailBox.x + detailBox.width).toBeLessThanOrEqual(width);
      await expect(details.getByRole('button', { name: 'Reindex' })).toBeVisible();
      await expect(details.getByRole('button', { name: 'Remove' })).toBeVisible();

      await page.getByRole('button', { name: 'Back to information' }).click();
      await expect(details).toBeHidden();
      await expect(assets.getByRole('button', { name: /acme\/service-a/ })).toBeFocused();
    });
  }
});

// Product Context shares the existing screen, but the collection belongs to
// the Product across all its delivery Projects.
async function mockProductWorkspace(page: Page) {
  await mockAssetsApi(page);
  await page.route('**/v1/products/product-one/profile', route => route.fulfill({ json: {
    product_id: 'product-one', revision: 1, base_project_id: 'project-a', can_edit: true,
    resources: [], environments: [], not_applicable: [], knowledge_count: 1,
    completion: { completed: 2, applicable: 5, percentage: 40, sections: [] },
  } }));
  await page.route('**/agent-context/products/product-one/sources', (route) => route.fulfill({
    json: { product_id: 'product-one', items: [], after: null },
  }));
  await page.route('**/api/features', (route) => route.fulfill({
    json: { features: { knowledge: true, product_profile: true } },
  }));
  await page.route('**/v1/products', (route) => route.fulfill({ json: {
    items: [{ id: 'product-one', name: 'Planner', revision: 1,
      project_ids: ['project-a', 'project-b'],
      projects: [{ id: 'project-a', name: 'Web' }, { id: 'project-b', name: 'Mobile' }],
    }], next_cursor: null,
  } }));
  await page.route('**/v1/products/product-one/projects/*', (route) => {
    const projectId = new URL(route.request().url()).pathname.split('/').pop();
    return route.fulfill({ json: {
      product_id: 'product-one', project_id: projectId, tenant_id: 'org-001', revision: 1,
      configuration: {},
      project: { id: projectId, name: projectId === 'project-a' ? 'Web' : 'Mobile', owner_user_id: 'owner-one' },
      product: { name: 'Planner', purpose: 'Plan delivery', intended_users: ['Teams'],
        owner_ref: { kind: 'user', id: 'owner-one' } },
    } });
  });
  await page.route('**/v1/products/product-one/projects/*/people?*', (route) => route.fulfill({ json: {
    product_id: 'product-one', revision: 1,
    owner: { id: 'owner-one', name: 'Product owner', github_username: null },
    team: null, members: [], next_offset: null, is_owner: false,
    permissions: { manage_team: false, manage_people: false, enroll_github: false },
  } }));
  const productAsset = { ...sampleAssets[0], id: 'product-information', display_name: 'Shared architecture',
    product_id: 'product-one', owner_sub: null, registered_by: 'another-member', can_manage: false };
  await page.route('**/api/agent-context/assets?*', (route) => {
    const query = new URL(route.request().url()).searchParams;
    const items = query.get('scope') === 'product' ? [productAsset] : sampleAssets;
    return route.fulfill({ json: { items, total: items.length, page: 1, page_size: 20, has_more: false, quota: null } });
  });
  await page.route('**/api/agent-context/assets/product-information', (route) => route.fulfill({ json: productAsset }));
}

test.describe('Product Context', () => {
  test('selects a saved GitHub repository and manages service refresh on desktop and mobile', async ({ page }, testInfo) => {
    await injectAuth(page);
    await mockProductWorkspace(page);
    let included = false;
    let enabled = false;
    let revision = 1;
    const writes: unknown[] = [];
    await page.route('**/agent-context/products/product-one/sources', (route) => route.fulfill({ json: {
      product_id: 'product-one', after: null, items: [{
        binding_id: 'binding-one', name: 'fixture/service', source_ref: 'https://github.com/fixture/service',
        connection_name: 'fixture', connection_id: 'connection-one', project_name: 'Web',
        included, eligible: true, can_manage: included, keep_updated: enabled, revision,
        asset_id: included ? 'source-asset' : undefined, status: included ? 'complete' : undefined,
        last_refreshed_at: included ? '2026-01-01T00:00:00Z' : null,
      }],
    } }));
    await page.route('**/agent-context/products/product-one/sources/binding-one', route => {
      const body = route.request().postDataJSON();
      writes.push(body);
      included = true;
      enabled = body.keep_updated;
      return route.fulfill({ status: 201, json: { ...sampleAssets[0], id: 'source-asset', product_id: 'product-one', owner_sub: null } });
    });
    await page.route('**/agent-context/assets/source-asset/refresh', route => {
      const body = route.request().postDataJSON();
      writes.push(body);
      enabled = body.enabled;
      revision += 1;
      return route.fulfill({ json: { enabled, revision } });
    });
    await page.goto(`${BASE_URL}/products/product-one/projects/project-a`);
    await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
    const panel = page.getByRole('region', { name: 'Product repositories' });
    await expect(panel.getByText('Connection: fixture · ADP GitHub App')).toBeVisible();
    await panel.getByRole('checkbox', { name: 'Keep updated: fixture/service' }).check();
    await panel.getByRole('button', { name: 'Include fixture/service in Context' }).click();
    await expect(panel.getByText(/Daily updates enabled/)).toBeVisible();
    expect(writes[0]).toEqual({ source_ref: 'https://github.com/fixture/service', keep_updated: true });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath('product-context-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 900 });
    await expect(panel.getByText(/Last successful refresh:/)).toBeVisible();
    const bounds = (await panel.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
    const saved = page.waitForResponse(response => response.url().endsWith('/agent-context/assets/source-asset/refresh')
      && response.request().method() === 'PATCH');
    await panel.getByRole('checkbox', { name: 'Keep updated: fixture/service' }).uncheck();
    expect((await saved).status()).toBe(200);
    await expect(panel.getByText(/Automatic updates off/)).toBeVisible();
    expect(writes[1]).toEqual({ enabled: false, revision: 1 });
    await expect(panel.getByText('Saving…')).toHaveCount(0);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath('product-context-mobile.png'), fullPage: true });
  });

  test('shares one collection across delivery Projects and keeps personal Context separate', async ({ page }) => {
    await injectAuth(page);
    await mockProductWorkspace(page);
    const queries: URLSearchParams[] = [];
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.pathname.endsWith('/api/agent-context/assets') && request.method() === 'GET') queries.push(url.searchParams);
    });
    await page.goto(`${BASE_URL}/products/product-one/projects/project-a`);
    await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
    await expect(page.getByText('Shared architecture', { exact: true })).toBeVisible();
    await expect(page.getByText('Shared with the Product team and platform admins.')).toBeVisible();
    await expect(page.getByRole('complementary', { name: 'Product summary' })).toBeHidden();
    await expect(page.getByRole('tab')).toHaveCount(0);

    await page.getByRole('combobox', { name: 'Project', exact: true }).selectOption('project-b');
    await expect(page).toHaveURL(/\/projects\/project-b\/knowledge$/);
    await expect(page.getByRole('button', { name: 'Knowledge', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByText('Shared architecture', { exact: true })).toBeVisible();
    expect(queries.length).toBeGreaterThan(0);
    expect(queries.every((query) => query.get('scope') === 'product' && query.get('product_id') === 'product-one' && !query.has('project_id'))).toBe(true);

    await page.goto(`${BASE_URL}/knowledge`);
    await expect(page.getByText('Your personal information, available only to you.')).toBeVisible();
    await expect(page.getByText('Shared architecture', { exact: true })).toHaveCount(0);
    expect(queries.at(-1)?.get('scope')).toBe('personal');
    expect(queries.at(-1)?.has('product_id')).toBe(false);
  });

  test('adds information in Product scope and shows management actions only when permitted', async ({ page }) => {
    await injectAuth(page);
    await mockProductWorkspace(page);
    await page.goto(`${BASE_URL}/products/product-one/projects/project-a`);
    await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
    await page.getByRole('button', { name: /Shared architecture/ }).click();
    const details = page.getByRole('region', { name: 'Information details' });
    await expect(details.getByRole('heading', { name: 'Shared architecture' })).toBeVisible();
    await expect(details.getByRole('button', { name: 'Reindex' })).toHaveCount(0);
    await expect(details.getByRole('button', { name: 'Remove' })).toHaveCount(0);

    await page.getByRole('button', { name: 'Add information', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Add information' });
    await expect(dialog.getByText('Your Product team and platform admins can access information added here.')).toBeVisible();
    await dialog.getByLabel('Link', { exact: true }).fill('https://docs.example.com/architecture');
    const created = page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/api/agent-context/assets'));
    await dialog.getByRole('button', { name: 'Add information', exact: true }).click();
    expect((await created).postDataJSON()).toEqual({ asset_type: 'url', source_ref: 'https://docs.example.com/architecture', scope: 'product', product_id: 'product-one' });
    await expect(dialog).toBeHidden();
  });

  test('fits mobile and reuses link-list import in the Add information dialog', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 900 });
    await injectAuth(page);
    await mockProductWorkspace(page);
    await page.goto(`${BASE_URL}/products/product-one/projects/project-a`);
    await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
    const information = page.getByRole('region', { name: 'Information', exact: true });
    await expect(information.getByText('Shared architecture', { exact: true })).toBeVisible();
    const bounds = (await information.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
    await expect(information.locator(':scope > div')).toHaveCSS('background-color', 'rgb(255, 255, 255)');

    await page.getByRole('button', { name: 'Add information', exact: true }).click();
    await page.getByRole('button', { name: 'Import a list of links' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(1);
    const dialog = page.getByRole('dialog', { name: 'Import a list of links' });
    await expect(dialog.getByText('Your Product team and platform admins can access information added here.')).toBeVisible();
    await expect(dialog.locator('input[type=file]')).toHaveCount(1);
  });
});

test('Product storage uploads privately and retries completion on desktop and mobile', async ({ page }, testInfo) => {
  await injectAuth(page);
  await mockProductWorkspace(page);
  const base = '**/agent-context/products/product-one/storage';
  await page.route(base, route => route.fulfill({ json: {
    product_id: 'product-one', bucket: 'example-context', prefix: 'tenants/example/products/product-one/',
    location: 's3://example-context/tenants/example/products/product-one/', revision: 0, encryption: 'shared',
    kms_key_arn: 'arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000',
    can_configure: true, can_upload: true, max_upload_bytes: 52428800, extensions: ['.md'],
  } }));
  let uploads = 0;
  let completions = 0;
  await page.route(base + '/uploads', route => {
    const body = route.request().postDataJSON();
    expect(body.filename).toBe('guide.md');
    expect(body.sha256).toMatch(/^[a-f0-9]{64}$/);
    return route.fulfill({ status: 201, json: { product_id: 'product-one', upload_id: 'receipt',
      source_ref: 's3://example-context/tenants/example/products/product-one/uploads/receipt/guide.md',
      url: 'https://storage.example.test/upload', headers: { 'Content-Type': 'text/markdown' }, expires_in: 300 } });
  });
  await page.route('https://storage.example.test/upload', route => {
    expect(route.request().method()).toBe('PUT');
    expect(route.request().headers().authorization).toBeUndefined();
    expect(route.request().headers().cookie).toBeUndefined();
    uploads += 1;
    return route.fulfill({ status: 200, body: '' });
  });
  await page.route(base + '/uploads/receipt/complete', route => {
    completions += 1;
    return completions === 1
      ? route.fulfill({ status: 503, json: { detail: 'Please retry completion.' } })
      : route.fulfill({ json: { id: 'uploaded-doc', product_id: 'product-one', status: 'queued' } });
  });
  await page.goto(`${BASE_URL}/products/product-one/projects/project-a`);
  await page.getByRole('button', { name: 'Knowledge', exact: true }).click();
  await page.getByRole('button', { name: 'Open storage' }).click();
  const dialog = page.getByRole('dialog', { name: 'Product Context storage' });
  await expect(dialog.getByText('Product location', { exact: true })).toBeVisible();
  await dialog.getByRole('combobox').selectOption('customer');
  await expect(dialog.getByLabel('Product KMS key ARN')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('product-storage-desktop.png'), fullPage: true });
  await dialog.getByLabel('Document', { exact: true }).setInputFiles({ name: 'guide.md', mimeType: 'text/markdown', buffer: Buffer.from('# Architecture') });
  await dialog.getByRole('button', { name: 'Upload document' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('Please retry completion.');
  await dialog.getByRole('button', { name: 'Finish import' }).click();
  await expect(dialog.getByText(/Document added to Product Context/)).toBeVisible();
  expect(uploads).toBe(1);
  expect(completions).toBe(2);
  await page.setViewportSize({ width: 390, height: 900 });
  const bounds = (await dialog.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: testInfo.outputPath('product-storage-mobile.png'), fullPage: true });
});
