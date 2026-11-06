import { test, expect, type Page } from '@playwright/test';

for (const width of [390, 1440]) {
  test(`Jira in existing connections at ${width}px`, async ({ page }, testInfo) => {
    const api = await installFixture(page);
    const scope = '/api/v1/products/product-1/projects/project-1';
    api.profiles.set('product-1', { tenant_id: 'tenant-a', product_id: 'product-1', project_id: 'project-1', revision: 1, configuration: {},
      product: { name: 'Release Planner', purpose: 'Plan releases', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } } });
    const source = { id: 'jira-source', revision: 1, connection_id: 'jira-1', site_id: 'site', label: 'App', jira_project_id: '100', jira_project_key: 'APP', board_id: null };
    let linked = false;
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const respond = (body: unknown) => route.fulfill({ json: body });
      if (url.pathname === `${scope}/sources`) return respond({ items: [{ connection_ref: { kind: 'github_installation', id: 'github-1' }, provider: 'github', label: 'Engineering GitHub', capabilities: ['repository'] }], next_cursor: null });
      if (url.pathname === `${scope}/bindings`) return respond({ items: [] });
      if (url.pathname === `${scope}/jira-context/connections`) return respond({ connections: [{ id: 'jira-1', label: 'Team Jira', site_id: 'site' }] });
      if (url.pathname === `${scope}/jira-context/connections/jira-1/resources`) return respond({ data: { values: url.searchParams.get('kind') === 'projects' ? [{ id: '100', key: 'APP', name: 'App' }] : [], isLast: true } });
      if (url.pathname === `${scope}/jira-context/sources`) {
        if (route.request().method() === 'GET') return respond({ sources: linked ? [source] : [] });
        expect(route.request().postDataJSON()).toEqual({ connection_id: 'jira-1', jira_project_id: '100', jira_project_key: 'APP', board_id: null, share_with_product: true });
        linked = true;
        return respond(source);
      }
      if (url.pathname === `${scope}/jira-context/read`) {
        expect(route.request().postDataJSON()).toMatchObject({ product_id: 'product-1', project_id: 'project-1', source_id: 'jira-source', view: 'issues' });
        return respond({ source, view: 'issues', data: { issues: [{ id: '200', key: 'APP-1', fields: { summary: 'Plan next release' } }] }, retrieved_at_ms: 1, next_cursor: null, coverage: 'provider_response' });
      }
      return route.fallback();
    });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/products/product-1/projects/project-1');
    await page.getByRole('button', { name: 'Manage connections', exact: true }).click();
    const connections = page.getByRole('region', { name: 'Project sources', exact: true });
    const jiraRow = connections.getByRole('button', { name: 'Browse Team Jira', exact: true });
    await expect(jiraRow).toBeVisible();
    await expect(jiraRow.locator('svg').first()).toBeVisible();
    await expect(connections.getByRole('button', { name: 'Browse Engineering GitHub' })).toBeVisible();
    await expect(connections.getByRole('heading', { name: 'Jira context' })).toHaveCount(0);
    await expect(connections.getByText('Shared Jira access for this Product', { exact: true })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`jira-list-${width}.png`), fullPage: true });
    await jiraRow.click();
    await connections.getByRole('combobox', { name: 'Jira project', exact: true }).selectOption('100');
    const save = connections.getByRole('button', { name: 'Connect Jira source' });
    await expect(save).toBeDisabled();
    await connections.getByRole('checkbox', { name: /Share this source/ }).check();
    await save.click();
    await expect(jiraRow).toContainText('1 linked');
    await connections.getByRole('button', { name: 'Read issues', exact: true }).click();
    await expect(connections.getByText('APP-1: Plan next release')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`jira-browse-${width}.png`), fullPage: true });
    await jiraRow.click();
    await expect(connections.getByRole('combobox', { name: 'Jira project', exact: true })).toHaveCount(0);
    await connections.getByRole('button', { name: 'Add connection', exact: true }).click();
    await page.getByRole('button', { name: 'Jira', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Connect Jira', exact: true })).toBeVisible();
  });
}

for (const width of [390, 1440]) {
  test(`project navigation and 135 workflows at ${width}px`, async ({ page }, testInfo) => {
    const api = await installFixture(page);
    const scope = '/api/v1/products/product-1/projects/project-1';
    const profile = { tenant_id: 'tenant-a', product_id: 'product-1', project_id: 'project-1', revision: 1, configuration: {},
      product: { name: 'Release Planner', purpose: 'Plan releases', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } },
      project: { id: 'project-1', name: 'General', owner_user_id: 'actor-a' } };
    api.profiles.set('product-1', profile);
    const projects = [{ id: 'project-1', name: 'General', is_base: true }, { id: 'project-2', name: 'Mobile release' }];
    const connection = { connection_ref: { kind: 'github_installation', id: 'connection-1' }, provider: 'github', label: 'Engineering', health: 'healthy', coverage: 'complete', capabilities: ['repository', 'pipeline'] };
    const repository = { kind: 'repository', name: 'release-planner', native_id: '10', namespace: '20', source_revision: 'repo-rev' };
    const workflows = Array.from({ length: 135 }, (_, index) => ({ kind: 'pipeline', name: `Release ${index + 1}`, native_id: String(1000 + index), namespace: '20', repository_id: '10', source_revision: `rev-${index}` }));
    const bindings: unknown[] = [];
    let writes = 0;
    let projectCreated = false;
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/api/v1/products' && route.request().method() === 'GET') return respond({ items: [{ id: 'product-1', name: profile.product.name, revision: profile.revision, project_ids: projects.map(p => p.id), projects, base_project_id: 'project-1' }], next_cursor: null });
      if (url.pathname === '/api/v1/products/product-1/projects' && route.request().method() === 'POST') {
        expect(route.request().postDataJSON()).toEqual({ mode: 'create', name: 'Next release' });
        projects.push({ id: 'project-3', name: 'Next release' });
        projectCreated = true;
        return respond({ ...profile, project_id: 'project-3', project: { id: 'project-3', name: 'Next release' }, revision: 2 });
      }
      if (url.pathname === '/api/v1/products/product-1/projects/project-3' && projectCreated) return respond({ ...profile, project_id: 'project-3', project: { id: 'project-3', name: 'Next release' }, revision: 2 });
      if (url.pathname.endsWith('/people')) return respond({ product_id: 'product-1', revision: 1, owner: { id: 'actor-a', name: 'Example owner' }, team: null, members: [], next_offset: null, is_owner: true, permissions: {} });
      if (url.pathname === `${scope}/sources`) {
        if (url.searchParams.has('pipeline_repository_id')) {
          const index = Number(url.searchParams.get('resource_cursor') ?? 1);
          return respond({ items: [{ ...connection, resources: workflows.slice((index - 1) * 50, index * 50), resource_cursor: index < 3 ? String(index + 1) : undefined }] });
        }
        return respond({ items: [url.searchParams.has('connection_id') ? { ...connection, resources: [repository] } : connection], next_cursor: null });
      }
      if (url.pathname === `${scope}/bindings`) {
        if (route.request().method() === 'GET') return respond({ items: bindings });
        expect(route.request().postDataJSON()).toMatchObject({ kind: 'pipeline', native_id: '1124', repository_id: '10', source_revision: 'rev-124' });
        writes += 1;
        const binding = { id: 'binding-1', project_id: 'project-1', status: 'healthy', selection: route.request().postDataJSON() };
        bindings.push(binding);
        return respond(binding);
      }
      return route.fallback();
    });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/products/product-1/projects/project-1');
    const workspace = page.getByRole('navigation', { name: 'Product workspace', exact: true });
    await expect(workspace.getByRole('button', { name: 'Profile', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('region', { name: 'Product details and team', exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Code repositories', exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Project sources', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Product settings', exact: true })).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: 'Profile setup sections', exact: true })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`profile-${width}.png`), fullPage: true });
    await page.goto('/products/product-1/projects/project-1/projects');
    await expect(page).toHaveURL(/\/projects\/project-1#product-projects$/);
    const projectsRegion = page.getByRole('region', { name: 'Product projects' });
    await expect(projectsRegion.getByText('General', { exact: true })).toBeVisible();
    await expect(projectsRegion.getByText('Mobile release', { exact: true })).toBeVisible();
    await expect(projectsRegion.getByText('Base', { exact: true })).toBeVisible();
    await projectsRegion.getByRole('button', { name: 'New project', exact: true }).click();
    const create = page.getByRole('dialog', { name: 'New project' });
    await create.getByRole('textbox', { name: 'New project name' }).fill('Next release');
    await create.getByRole('button', { name: 'Create project', exact: true }).click();
    await expect(page.getByRole('combobox', { name: 'Project', exact: true })).toHaveValue('project-3');
    await expect(projectsRegion.getByText('Next release', { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`projects-${width}.png`) });
    await page.getByRole('combobox', { name: 'Project', exact: true }).selectOption('project-1');
    await page.goto('/products/product-1/projects/project-1/settings?section=advanced');
    await expect(page).toHaveURL(/\/projects\/project-1#product-connections$/);
    await page.getByRole('button', { name: 'Browse Engineering', exact: true }).click();
    await page.getByRole('button', { name: 'Browse workflows' }).click();
    const picker = page.getByRole('dialog', { name: 'Workflows in release-planner' });
    await expect(picker.getByText('1–10 of 135 workflows')).toBeVisible();
    await expect(picker.getByRole('listitem')).toHaveCount(10);
    expect(writes).toBe(0);
    await picker.getByRole('button', { name: 'Next', exact: true }).click();
    await expect(picker.getByText('11–20 of 135 workflows')).toBeVisible();
    await picker.getByRole('searchbox', { name: 'Search workflows' }).fill('Release 125');
    await picker.getByRole('button', { name: 'Link to project', exact: true }).click();
    await expect(picker.getByRole('button', { name: 'Linked', exact: true })).toBeDisabled();
    await expect(picker.getByRole('status')).toHaveText('Linked workflow to this project.');
    await expect(picker.getByRole('status')).toBeInViewport();
    expect(writes).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`workflows-${width}.png`) });
    await picker.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Browse workflows' })).toBeFocused();
  });
}

for (const width of [390, 1440]) {
  test(`product connections at ${width}px show actions and feedback beside the source`, async ({ page }, testInfo) => {
    const api = await installFixture(page);
    const scope = '/api/v1/products/product-1/projects/project-1';
    api.profiles.set('product-1', {
      tenant_id: 'tenant-a', product_id: 'product-1', project_id: 'project-1', revision: 1, configuration: {},
      product: { name: 'Release Planner', purpose: 'Plan releases', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } },
    });
    const connections = Array.from({ length: 8 }, (_, index) => ({
      connection_ref: { kind: 'github_installation', id: `connection-${index}` }, provider: 'github',
      label: `GitHub ${index + 1}`, health: 'healthy', coverage: 'complete', capabilities: ['repository'],
    }));
    const resources = Array.from({ length: 10 }, (_, index) => ({
      kind: 'repository', namespace: 'example-org', native_id: `repo-${index}`, name: `repository-${index}`, source_revision: 'revision-1',
    }));
    let releaseDiscovery!: () => void;
    const discovery = new Promise<void>(resolve => { releaseDiscovery = resolve; });
    let releaseSelection!: () => void;
    const selection = new Promise<void>(resolve => { releaseSelection = resolve; });
    const binding = { id: 'binding-1', project_id: 'project-1', status: 'healthy', selection: { kind: 'repository', native_id: 'repo-0', namespace: 'example-org', provider: 'github', connection_ref: connections[0].connection_ref } };
    let selected = false;
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === `${scope}/sources`) {
        if (!url.searchParams.has('connection_id')) return respond({ items: connections, next_cursor: null });
        await discovery;
        return respond({ items: [{ ...connections[0], resources }] });
      }
      if (url.pathname === `${scope}/bindings`) {
        if (route.request().method() === 'GET') return respond({ items: selected ? [binding] : [] });
        expect(route.request().postDataJSON()).toMatchObject({ kind: 'repository', native_id: 'repo-0', connection_ref: connections[0].connection_ref });
        await selection;
        selected = true;
        return respond(binding);
      }
      return route.fallback();
    });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/products/product-1/projects/project-1');
    await page.getByRole('button', { name: 'Manage connections', exact: true }).click();
    const sources = page.getByRole('region', { name: 'Project sources', exact: true });
    await sources.getByRole('button', { name: 'Add connection', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Add connection', exact: true });
    await expect(dialog.getByRole('button', { name: 'AWS account', exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Close modal' }).click();
    const inspect = sources.getByRole('button', { name: /Browse GitHub 1/ });
    await inspect.click();
    await expect(sources.getByText('Loading sources…', { exact: true })).toBeInViewport();
    await expect(inspect).toBeDisabled();
    releaseDiscovery();
    const resource = sources.getByRole('group', { name: 'repository: repository-0', exact: true });
    await resource.getByRole('button', { name: 'Link repository to project', exact: true }).click();
    await expect(resource.getByRole('status')).toHaveText('Linking repository…');
    await expect(resource.getByRole('status')).toBeInViewport();
    releaseSelection();
    await expect(resource.getByRole('status')).toHaveText('Linked repository to this project.');
    await expect(resource.getByRole('button', { name: 'Linked', exact: true })).toBeDisabled();
    await expect(resource.getByRole('status')).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`connections-${width}.png`) });
    await sources.getByRole('button', { name: 'Add connection', exact: true }).click();
    await dialog.getByRole('button', { name: 'GitHub', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Connect GitHub', exact: true })).toBeVisible();
    await expect(page).toHaveURL(/\/products\/product-1\/projects\/project-1$/);
  });
}

for (const width of [390, 1440]) {
  test(`financial planning at ${width}px preserves shared Profile completion`, async ({ page }, testInfo) => {
    const api = await installFixture(page);
    const scope = '/api/v1/products/product-1/projects/project-1';
    const profile = {
      tenant_id: 'tenant-a', product_id: 'product-1', project_id: 'project-1', revision: 1,
      product: { name: 'Release Planner', purpose: 'Plan the next release', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } },
      project: { id: 'project-1', name: 'Main delivery', owner_user_id: 'actor-a' },
      configuration: {} as Record<string, unknown>,
      progress: { completed: 2, applicable: 3, percentage: 66, maturity: 'saved' },
    };
    api.profiles.set('product-1', profile);
    const session = {
      id: 'financial-session', product_id: 'product-1', project_id: 'project-1', revision: 1, state: 'open', proposals: [],
      steps: { information: { status: 'complete' }, basic: { status: 'complete' }, financials: { status: 'not_started' } },
      progress: profile.progress,
    };
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      const respond = (value: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
      if (path === `${scope}/changes`) return respond({ items: [{ event_id: 'change-1', revision: profile.revision }] });
      if (path === `${scope}/onboarding-sessions` || path === `${scope}/onboarding-sessions/financial-session`) return respond(session);
      if (path === `${scope}/onboarding-sessions/financial-session/steps/financials`) {
        expect(route.request().method()).toBe('PUT');
        profile.configuration = route.request().postDataJSON().configuration;
        profile.revision = 2;
        profile.progress = { completed: 3, applicable: 3, percentage: 100, maturity: 'saved' };
        session.revision = 2;
        session.progress = profile.progress;
        session.steps.financials.status = 'complete';
        return respond(session);
      }
      if (path.includes('/initiatives') || path.includes('/contributions')) return respond({ items: [] });
      return route.fallback();
    });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/products/product-1/projects/project-1');
    await page.getByRole('button', { name: 'Profile', exact: true }).click();
    await page.getByRole('button', { name: 'Budget & spend', exact: true }).click();
    await page.getByText('Financial planning', { exact: true }).click();
    await page.getByRole('button', { name: 'Start or resume onboarding', exact: true }).click();
    const form = page.getByRole('form', { name: 'Financials', exact: true });
    await expect(form).toBeVisible();
    await expect(page.getByLabel('Ask for guidance', { exact: true })).not.toBeVisible();
    await expect(page.getByText('Session financial-session', { exact: false })).not.toBeVisible();
    await form.getByLabel('Name', { exact: true }).fill('Cloud plan');
    await form.getByLabel('Amount', { exact: true }).fill('75.00');
    await form.getByRole('combobox', { name: 'Category', exact: true }).selectOption('cloud');
    await form.getByLabel('Period start', { exact: true }).fill('2026-10-01');
    await form.getByLabel('Period end', { exact: true }).fill('2027-03-31');
    await form.getByLabel('Release target date', { exact: true }).fill('2027-01-18');
    await form.getByRole('button', { name: 'Save proposed allocation' }).click();
    await page.getByRole('button', { name: 'Profile', exact: true }).click();
    await expect(page.getByRole('progressbar', { name: 'Profile completion' })).toHaveAttribute('value', '20');
    await page.getByRole('button', { name: 'Budget & spend', exact: true }).click();
    await expect(form.getByLabel('Amount', { exact: true })).toHaveValue('75.00');
    await page.getByText('Optional guidance', { exact: true }).click();
    await expect(page.getByLabel('Ask for guidance', { exact: true })).toBeVisible();
    await page.getByText('Optional guidance', { exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath(`financials-${width}.png`), fullPage: true });
  });
}

type Profile = {
  tenant_id: string;
  configuration: Record<string, unknown>;
  product_id: string;
  project_id: string;
  revision: number;
  product: { name: string; purpose: string; intended_users: string[]; owner_ref: { kind: string; id: string } };
};

async function installFixture(page: Page) {
  const profiles = new Map<string, Profile>();
  const receipts = new Map<string, Profile>();
  let reads = 0;
  let dropNextResponse = false;
  const createReceipts: string[] = [];
  await page.addInitScript(() => {
    const now = Math.floor(Date.now() / 1000);
    const token = `${btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))}.${btoa(JSON.stringify({
      sub: 'actor-a', email: 'owner@example.test', name: 'Example owner', auth_time: now,
      'custom:role': 'member', 'custom:org_id': 'tenant-a', exp: now + 3600,
    }))}.${btoa('test-signature')}`;
    sessionStorage.setItem('cognito_access_token', token);
    sessionStorage.setItem('cognito_id_token', token);
    sessionStorage.setItem('cognito_token_expiry', String((now + 3600) * 1000));
  });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const respond = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    const sharedProduct = path.match(/^\/api\/v1\/products\/([^/]+)\/profile$/);
    if (sharedProduct && method === 'GET') {
      const product = profiles.get(sharedProduct[1]);
      return product ? respond({ product_id: product.product_id, revision: product.revision, base_project_id: product.project_id,
        can_edit: true, resources: [], environments: [], not_applicable: [], knowledge_count: 0,
        completion: { completed: 1, applicable: 5, percentage: 20, sections: [
          { id: 'details', status: 'complete' }, ...['repositories', 'environments', 'backlog', 'knowledge'].map(id => ({ id, status: 'missing' })),
        ] } }) : respond({ detail: 'not_found' }, 404);
    }
    if (path === '/api/features') return respond({ features: { product_profile: true } });
    if (path === '/api/access/status') return respond({ status: 'registered' });
    if (path === '/api/auth/workspaces') return respond({ items: [{ org_id: 'tenant-a', name: 'Example organization' }] });
    if (path === '/api/v1/projects/project-1/budget') return respond({ product_id: 'product-1', project_id: 'project-1', project_name: 'Main delivery', is_base: true, budget_usd: 100, spent_usd: 0, reserved_usd: 0, available_usd: 100, accounting_status: 'current', can_manage: false, flows: [] });
    if (path.endsWith('/people')) return respond({ product_id: 'product-1', revision: 1, owner: { id: 'actor-a', name: 'Example owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    if (path === '/api/v1/products' && method === 'POST') {
      const body = route.request().postDataJSON();
      const receipt = route.request().headers()['idempotency-key'];
      if (!receipt) return respond({ detail: { code: 'validation_error' } }, 422);
      createReceipts.push(receipt);
      let profile = receipts.get(receipt);
      if (!profile) {
        profile = {
          tenant_id: 'tenant-a', product_id: 'product-1', project_id: 'project-1', revision: 1, configuration: {},
          product: { name: body.name, purpose: body.purpose, intended_users: body.intended_users, owner_ref: { kind: 'user', id: 'actor-a' } },
        };
        profiles.set(profile.product_id, profile);
        receipts.set(receipt, profile);
      }
      if (dropNextResponse) {
        dropNextResponse = false;
        return route.abort('failed');
      }
      return respond(profile, 201);
    }
    if (path === '/api/v1/products' && method === 'GET') {
      return respond({ items: [...profiles.values()].map((profile) => ({
        id: profile.product_id, name: profile.product.name, revision: profile.revision, project_ids: [profile.project_id],
      })), next_cursor: null });
    }
    if (path === '/api/v1/products/product-1/projects/project-1' && method === 'GET') {
      reads += 1;
      return profiles.has('product-1') ? respond(profiles.get('product-1')) : respond({ detail: 'not_found' }, 404);
    }
    return respond({ detail: 'not_found' }, 404);
  });
  return { profiles, receipts, createReceipts, readCount: () => reads, dropNextSaveResponse: () => { dropNextResponse = true; } };
}

for (const width of [390, 1440]) {
  test(`product delivery flows at ${width}px retain scope through filters and details`, async ({ page }, testInfo) => {
    const api = await installFixture(page);
    const profile = {
      tenant_id: 'tenant-a', product_id: 'product-1', project_id: 'project-1', revision: 1, configuration: {},
      product: { name: 'Release Planner', purpose: 'Plan the next release', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } },
    };
    api.profiles.set('product-1', profile);
    const cost = { status: 'known', amount_usd: '0.15', scope: 'agent run costs only; excludes build/infra' };
    const flow = {
      id: 'flow-1', slug: 'checkout-release', title: 'Checkout release', product_id: 'product-1', project_id: 'project-2',
      description: 'Ship the next checkout release', intent_ref: null, design_history: null, status: 'running',
      awaiting_gate_count: 0, stalled_count: 0, total_nodes: 3, story_count: 3, completed_story_count: 1,
      display_counts: { queued: 1, in_progress: 1, gate: 0, stalled: 0, complete: 1 },
      epic_count: 1, wave_count: 0, current_wave_ref: null, waves: [], delivery_cost: cost,
      created_at: '2026-10-28T00:00:00Z', updated_at: null,
    };
    const queries: URLSearchParams[] = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const respond = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      if (url.pathname === '/api/features') return respond({ features: { product_profile: true, orchestration_engine: true } });
      if (url.pathname === '/api/orchestration/flows') {
        queries.push(url.searchParams);
        expect(url.searchParams.get('product_id')).toBe('product-1');
        expect(url.searchParams.has('project_id')).toBe(false);
        return respond({ product_id: 'product-1', flows: [flow], total: 1, limit: 25, offset: 0,
          status_counts: { running: 1, queued: 0, complete: 0, attention_needed: 0, awaiting_you: 0, empty: 0, archived: 0 } });
      }
      if (url.pathname === '/api/orchestration/flows/flow-1') return respond({
        flow_id: flow.id, title: flow.title, slug: flow.slug, state: 'running', nodes: [], edges: [], cost,
        product_id: 'product-1', product_name: 'Release Planner', product_status: 'accessible',
      });
      return route.fallback();
    });
    await page.setViewportSize({ width, height: 1000 });
    const path = '/products/product-1/projects/project-1/flows';
    await page.goto('/products/product-1/projects/project-1');
    expect(queries).toHaveLength(0);
    await page.getByRole('button', { name: 'Delivery flows', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Checkout release' })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await page.getByTestId('status-chip-running').click();
    await expect(page).toHaveURL(new RegExp(`${path}\\?status=running$`));
    await expect.poll(() => queries.at(-1)?.get('status')).toBe('running');
    await expect(page.getByRole('link', { name: 'New flow', exact: true })).toHaveAttribute('href', '/flows/new?product=product-1');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.getByTestId('needs-me-filter').locator('..')).toHaveCSS('display', 'flex');
    await page.screenshot({ path: testInfo.outputPath(`product-flows-${width}.png`), fullPage: true });
    await page.getByTestId('flow-card-flow-1').click();
    await expect(page.getByRole('heading', { name: 'Checkout release' })).toBeVisible();
    await page.getByRole('link', { name: '← Delivery flows', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${path}\\?status=running$`));
    await expect(page.getByRole('button', { name: 'Delivery flows', exact: true })).toHaveAttribute('aria-current', 'page');
    await page.reload();
    await expect(page.getByTestId('flow-card-flow-1')).toBeVisible();
  });
}

test('ui_minimal_save: create from three fields, independently read and reload', async ({ page }) => {
  const api = await installFixture(page);
  await page.goto('/products');
  await expect(page.getByRole('heading', { name: 'Products' })).toBeVisible();
  await page.getByRole('link', { name: 'Create product' }).click();
  await page.getByRole('textbox', { name: 'Name' }).fill('Example Planner');
  await page.getByRole('textbox', { name: 'Purpose' }).fill('Coordinate small releases');
  await page.getByRole('textbox', { name: 'Intended users' }).fill('Maintainers');
  await expect(page.getByRole('combobox', { name: /delivery scope/i })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: /portfolio|repository|budget/i })).toHaveCount(0);
  await page.getByRole('button', { name: 'Save product' }).click();
  await expect(page.getByRole('heading', { name: 'Example Planner' })).toBeVisible();
  expect(api.readCount()).toBeGreaterThan(0);
  expect(api.receipts.size).toBe(1);
  expect(api.profiles.get('product-1')?.product).toEqual({
    name: 'Example Planner', purpose: 'Coordinate small releases', intended_users: ['Maintainers'],
    owner_ref: { kind: 'user', id: 'actor-a' },
  });
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Example Planner' })).toBeVisible();
  await page.getByText('CLI & saved details').click();
  await expect(page.getByText('Saved revision 1')).toBeVisible();
  expect(api.readCount()).toBeGreaterThan(1);
});

for (const width of [390, 1440]) {
  test(`ui_input_recovery at ${width}px: keyboard validation, offline reply and no horizontal overflow`, async ({ page }) => {
    const api = await installFixture(page);
    await page.setViewportSize({ width, height: 900 });
    const assertNoOverflow = async () => {
      const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }));
      expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
    };
    await page.goto('/products/new');
    await expect(page.getByRole('heading', { name: 'Create product' })).toBeVisible();
    await assertNoOverflow();
    await page.getByRole('button', { name: 'Save product' }).click();
    await expect(page.getByRole('textbox', { name: 'Name' })).toBeFocused();
    await expect(page.getByRole('alert')).toContainText('Check the highlighted fields');
    await page.getByRole('textbox', { name: 'Name' }).fill('Example Planner');
    await page.getByRole('textbox', { name: 'Purpose' }).fill('Coordinate small releases');
    await page.getByRole('textbox', { name: 'Intended users' }).fill('Maintainers');
    api.dropNextSaveResponse();
    await page.getByRole('button', { name: 'Save product' }).click();
    await expect(page.getByRole('alert')).toContainText("couldn't confirm the save");
    await expect(page.getByRole('textbox', { name: 'Name' })).toHaveValue('Example Planner');
    await assertNoOverflow();
    await page.reload();
    await expect(page.getByRole('textbox', { name: 'Purpose' })).toHaveValue('Coordinate small releases');
    await page.getByRole('button', { name: 'Check save status' }).click();
    await expect(page.getByRole('heading', { name: 'Example Planner' })).toBeVisible();
    expect(api.createReceipts).toHaveLength(2);
    expect(new Set(api.createReceipts).size).toBe(1);
    expect(api.receipts.size).toBe(1);
    expect(api.readCount()).toBeGreaterThan(0);
    await assertNoOverflow();
  });
}

test('optional_portfolio_ui: optional dated collections survive reload without changing Product', async ({ page }) => {
  const api = await installFixture(page);
  type Membership = { relationship_id: string; relationship_revision: string; portfolio_ref: Record<string, string>;
    product_ref: Record<string, string>; valid_from: string; valid_to?: string };
  const records = [
    { id: 'portfolio-commerce', revision: '1', data: { name: 'Commerce' } },
    { id: 'portfolio-shared', revision: '1', data: { name: 'Shared services' } },
  ];
  const memberships = new Map(records.map((record) => [record.id, [] as Membership[]]));
  const respond = (route: import('@playwright/test').Route, value: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.route('**/api/v1/catalog/portfolios**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === '/api/v1/catalog/portfolios' && method === 'GET') return respond(route, { items: records, can_edit: true });
    const match = path.match(/^\/api\/v1\/catalog\/portfolios\/([^/]+)\/memberships(?:\/([^/]+))?$/);
    if (!match || !memberships.has(match[1])) return respond(route, { code: 'not_found' }, 404);
    const current = memberships.get(match[1])!;
    if (method === 'GET') return respond(route, { items: current.filter((item) => !item.valid_to || item.valid_to > '2026-10-20T00:00:00Z') });
    if (method === 'POST' && !match[2]) {
      const body = route.request().postDataJSON();
      if (!route.request().headers()['idempotency-key'] || body.product_ref.record_id !== 'product-1'
        || body.product_ref.record_revision !== '1' || body.portfolio_ref.record_revision !== '1') {
        return respond(route, { code: 'stale_revision' }, 412);
      }
      const relationship_id = `membership-${current.length + 1}-${match[1]}`;
      current.push({ relationship_id, relationship_revision: '1', portfolio_ref: body.portfolio_ref,
        product_ref: body.product_ref, valid_from: body.valid_from });
      return respond(route, { id: relationship_id, revision: '1', change_id: 'fixture-change' }, 201);
    }
    if (method === 'PATCH' && match[2]) {
      const item = current.find((entry) => entry.relationship_id === match[2]);
      if (!item || route.request().headers()['if-match'] !== item.relationship_revision) return respond(route, { code: 'stale_revision' }, 412);
      item.relationship_revision = '2';
      item.valid_to = route.request().postDataJSON().valid_to;
      return respond(route, { id: item.relationship_id, revision: '2', change_id: 'fixture-change' });
    }
    return respond(route, { code: 'invalid_request' }, 400);
  });

  await page.goto('/products/new');
  await page.getByRole('textbox', { name: 'Name' }).fill('Example Planner');
  await page.getByRole('textbox', { name: 'Purpose' }).fill('Coordinate small releases');
  await page.getByRole('textbox', { name: 'Intended users' }).fill('Maintainers');
  await page.getByRole('button', { name: 'Save product' }).click();
  await expect(page.getByRole('heading', { name: 'Example Planner' })).toBeVisible();
  expect(api.profiles.get('product-1')?.revision).toBe(1);
  expect([...memberships.values()].flat()).toHaveLength(0);
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await page.getByText('Organization and Portfolios').click();
  const organization = page.getByRole('region', { name: 'Portfolio organization' });
  await expect(organization.getByText('No visible Portfolio memberships for this Product.')).toBeVisible();
  for (const record of records) {
    await organization.getByRole('combobox', { name: 'Add to Portfolio' }).selectOption(record.id);
    await organization.getByLabel('Membership starts on').fill('2020-01-01');
    await organization.getByRole('button', { name: 'Add membership' }).click();
    await expect(organization.getByText(`${record.data.name}: 2020-01-01 to ongoing`)).toBeVisible();
    expect(memberships.get(record.id)?.[0]).toMatchObject({ valid_from: '2020-01-01T00:00:00Z',
      portfolio_ref: { record_id: record.id, record_revision: '1' },
      product_ref: { record_id: 'product-1', project_id: 'project-1', record_revision: '1' } });
  }
  await page.reload();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await page.getByText('Organization and Portfolios').click();
  await expect(organization.getByText('Commerce: 2020-01-01 to ongoing')).toBeVisible();
  await expect(organization.getByText('Shared services: 2020-01-01 to ongoing')).toBeVisible();
  await organization.getByLabel('End membership on').first().fill('2021-01-01');
  await organization.getByRole('button', { name: 'End Commerce membership' }).click();
  await expect(organization.getByText('Commerce: 2020-01-01 to ongoing')).toHaveCount(0);
  await expect(organization.getByText('Shared services: 2020-01-01 to ongoing')).toBeVisible();
  expect(memberships.get('portfolio-commerce')?.[0]).toMatchObject({ relationship_revision: '2', valid_to: '2021-01-01T00:00:00Z' });
  expect(api.profiles.get('product-1')?.product.name).toBe('Example Planner');
  expect(api.profiles.get('product-1')?.revision).toBe(1);
});

test('initiative_selection_ui: choose existing Initiative and view only this Product Contribution', async ({ page }) => {
  const api = await installFixture(page);
  const reference = { tenant_id: 'tenant-a', project_id: 'project-1', source_connection_id: 'adp.catalog',
    record_type: 'initiative', record_id: 'initiative-1', record_revision: '1' };
  const own = { id: 'contribution-1', revision: '1', project_id: 'project-1',
    data: { initiative_ref: reference, product_ref: { ...reference, source_connection_id: 'adp.profile',
      record_type: 'product', record_id: 'product-1' }, objective: 'Reduce checkout friction', work_links: [] } };
  const foreign = { ...own, id: 'contribution-2', data: { ...own.data,
    product_ref: { ...own.data.product_ref, record_id: 'product-2' }, objective: 'Another Product commitment' } };
  const contributions = [own, foreign];
  const session = { id: 'session-1', revision: 1, state: 'open', steps: {
    information: { status: 'complete' }, basic: { status: 'not_started' }, advanced: { status: 'not_started' },
    financials: { status: 'not_started' },
  }, progress: { completed: 2, applicable: 2, percentage: 100, maturity: 'saved' } };
  let savedReference: typeof reference | undefined;
  let catalogWrites = 0;
  const respond = (route: import('@playwright/test').Route, value: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.route('**/api/v1/catalog/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'GET') {
      catalogWrites += 1;
      return respond(route, { code: 'invalid_request' }, 400);
    }
    if (path === '/api/v1/catalog/initiatives') return respond(route, { items: [{ tenant_id: 'tenant-a',
      id: 'initiative-1', revision: '1', data: { name: 'Improve signup', status: 'planned' } }] });
    if (path === '/api/v1/catalog/projects/project-1/contributions') return respond(route, { items: contributions });
    return respond(route, { code: 'not_found' }, 404);
  });
  await page.route('**/api/v1/products/product-1/projects/project-1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path.endsWith('/changes') && method === 'GET') return respond(route, { items: [{ event_id: 'change-1', revision: 1 }] });
    if (path.endsWith('/onboarding-sessions') && method === 'POST') return respond(route, session, 201);
    if (path.endsWith('/onboarding-sessions/session-1') && method === 'GET') return respond(route, session);
    if (path.endsWith('/onboarding-sessions/session-1/steps/financials') && method === 'PUT') {
      const financials = route.request().postDataJSON().configuration.financials;
      savedReference = financials.initiatives[0];
      if (JSON.stringify(savedReference) !== JSON.stringify(reference)) return respond(route, { code: 'invalid_request' }, 422);
      const current = api.profiles.get('product-1')!;
      api.profiles.set('product-1', { ...current, revision: 2, configuration: { financials } });
      session.revision = 2;
      return respond(route, session);
    }
    return route.fallback();
  });

  await page.goto('/products/new');
  await page.getByRole('textbox', { name: 'Name' }).fill('Example Planner');
  await page.getByRole('textbox', { name: 'Purpose' }).fill('Coordinate small releases');
  await page.getByRole('textbox', { name: 'Intended users' }).fill('Maintainers');
  await page.getByRole('button', { name: 'Save product' }).click();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await page.getByRole('button', { name: 'Budget & spend', exact: true }).click();
  await page.getByText('Financial planning', { exact: true }).click();
  await page.getByRole('button', { name: 'Start or resume onboarding' }).click();
  const form = page.getByRole('form', { name: 'Financials' });
  await expect(form.getByRole('option', { name: 'Improve signup' })).toBeAttached();
  await form.getByRole('combobox', { name: 'Existing Initiative' }).selectOption({ label: 'Improve signup' });
  await expect(form.getByText('Contribution: Reduce checkout friction · Revision 1')).toBeVisible();
  await expect(form.getByText('Another Product commitment')).toHaveCount(0);
  await form.getByRole('textbox', { name: 'Name' }).fill('Cross-product proposal');
  await form.getByRole('textbox', { name: 'Amount' }).fill('75.00');
  await form.getByLabel('Period start').fill('2026-10-01');
  await form.getByLabel('Period end').fill('2026-12-31');
  await form.getByRole('button', { name: 'Save proposed allocation' }).click();
  await expect(form.getByText('Initiative: initiative-1')).toBeVisible();
  expect(savedReference).toEqual(reference);
  expect(contributions).toEqual([own, foreign]);
  expect(catalogWrites).toBe(0);
  expect(api.profiles.get('product-1')).toMatchObject({ product_id: 'product-1', project_id: 'project-1',
    product: { name: 'Example Planner' }, configuration: { financials: { initiatives: [reference] } } });
  expect(api.readCount()).toBeGreaterThan(1);
});

test('catalog_ui_failure_states: optional coverage fails safely while Product edits continue', async ({ page }) => {
  const api = await installFixture(page);
  const visible = { id: 'portfolio-visible', revision: '1', data: { name: 'Visible collection' } };
  const membership = { relationship_id: 'membership-visible', relationship_revision: '1',
    portfolio_ref: { record_id: visible.id }, product_ref: { record_id: 'product-1', project_id: 'project-1' },
    valid_from: '2020-01-01T00:00:00Z' };
  let unavailable = true;
  let stale = false;
  let revokedWrite = false;
  let revokedPage = false;
  let writes = 0;
  let productEdits = 0;
  const respond = (route: import('@playwright/test').Route, value: unknown, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.route('**/api/v1/catalog/portfolios**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === '/api/v1/catalog/portfolios' && method === 'GET') {
      if (unavailable) return respond(route, { code: 'dependency_unavailable' }, 503);
      if (url.searchParams.has('cursor')) return revokedPage
        ? respond(route, { code: 'permission_denied' }, 403) : respond(route, { items: [] });
      return respond(route, { items: [visible], next_cursor: revokedPage ? 'second-page' : undefined, can_edit: true });
    }
    if (path === `/api/v1/catalog/portfolios/${visible.id}/memberships` && method === 'GET') {
      return respond(route, { items: [membership] });
    }
    if (path === `/api/v1/catalog/portfolios/${visible.id}/memberships/${membership.relationship_id}` && method === 'PATCH') {
      writes++;
      if (revokedWrite) return respond(route, { code: 'permission_denied' }, 403);
      if (route.request().headers()['if-match'] !== membership.relationship_revision) return respond(route, { code: 'stale_revision' }, 412);
      if (!stale) {
        stale = true;
        membership.relationship_revision = '2';
        return respond(route, { code: 'stale_revision' }, 412);
      }
      return respond(route, { code: 'invalid_request' }, 400);
    }
    return respond(route, { code: 'not_found' }, 404);
  });
  await page.route('**/api/v1/products/product-1/projects/project-1', async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    const current = api.profiles.get('product-1')!;
    if (!route.request().headers()['idempotency-key'] || route.request().headers()['if-profile-revision'] !== String(current.revision)) {
      return respond(route, { code: 'stale_revision' }, 412);
    }
    const updated = { ...current, revision: current.revision + 1, product: { ...current.product, ...route.request().postDataJSON() } };
    api.profiles.set('product-1', updated);
    productEdits++;
    return respond(route, updated);
  });

  await page.goto('/products/new');
  await page.getByRole('textbox', { name: 'Name' }).fill('Catalog Fallback');
  await page.getByRole('textbox', { name: 'Purpose' }).fill('Initial purpose');
  await page.getByRole('textbox', { name: 'Intended users' }).fill('Maintainers');
  await page.getByRole('button', { name: 'Save product' }).click();
  await expect(page.getByRole('heading', { name: 'Catalog Fallback' })).toBeVisible();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await page.getByText('Organization and Portfolios').click();
  const organization = page.getByRole('region', { name: 'Portfolio organization' });
  await expect(organization.getByRole('alert')).toContainText('Portfolio coverage unavailable');
  await expect(organization.getByText('No visible Portfolio memberships for this Product.')).toHaveCount(0);
  await page.getByRole('textbox', { name: 'Purpose' }).fill('Edited while catalog was down');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('p[role="status"]', { hasText: 'Saved revision 2' })).toBeVisible();
  expect(productEdits).toBe(1);
  expect(api.profiles.get('product-1')?.product.purpose).toBe('Edited while catalog was down');

  unavailable = false;
  await organization.getByRole('button', { name: 'Retry' }).click();
  await expect(organization.getByText('Visible collection: 2020-01-01 to ongoing')).toBeVisible();
  await organization.getByLabel('End membership on').fill('2030-01-01');
  await organization.getByRole('button', { name: 'End Visible collection membership' }).click();
  await expect(organization.getByRole('alert')).toContainText('Portfolio details changed elsewhere');
  await expect(organization.getByText('Visible collection: 2020-01-01 to ongoing')).toHaveCount(0);
  expect(membership.relationship_revision).toBe('2');
  expect(writes).toBe(1);
  await organization.getByRole('button', { name: 'Refresh current Portfolios' }).click();
  await expect(organization.getByText('Visible collection: 2020-01-01 to ongoing')).toBeVisible();

  revokedWrite = true;
  await organization.getByLabel('End membership on').fill('2031-01-01');
  await organization.getByRole('button', { name: 'End Visible collection membership' }).click();
  await expect(organization.getByRole('alert')).toContainText('Portfolio access changed');
  await expect(organization.getByText(/Visible collection|Restricted collection|No visible Portfolio memberships/)).toHaveCount(0);
  expect(writes).toBe(2);

  revokedPage = true;
  await organization.getByRole('button', { name: 'Check access again' }).click();
  await expect(organization.getByRole('alert')).toContainText('Portfolio access changed');
  await expect(organization.getByText(/Visible collection|Restricted collection|No visible Portfolio memberships/)).toHaveCount(0);
  expect(api.profiles.get('product-1')?.revision).toBe(2);
  expect(api.readCount()).toBeGreaterThan(1);
});

for (const width of [390, 1440]) {
  test(`workspace_people at ${width}px: product team and GitHub member are visible and editable`, async ({ page }, testInfo) => {
    const api = await installFixture(page);
    api.profiles.set('product-1', { tenant_id: 'tenant-a', configuration: {}, product_id: 'product-1', project_id: 'project-1', revision: 1,
      product: { name: 'Example Planner', purpose: 'Help teams coordinate small releases.', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } } });
    let team = { id: 'platform', name: 'Platform team' };
    let githubMember = '';
    let revision = 1;
    await page.route('**/api/v1/products/product-1/projects/project-1/people*', route => route.fulfill({ json: {
      product_id: 'product-1', revision, owner: { id: 'actor-a', name: 'Maya Chen', github_username: 'maya' },
      team, members: [{ id: 'actor-a', name: 'Maya Chen', github_username: 'maya' }, ...(githubMember ? [{ id: 'new-person', name: 'New member', github_username: githubMember }] : [])],
      next_offset: null, is_owner: true, permissions: { manage_team: true, manage_people: true, enroll_github: true },
    } }));
    await page.route('**/api/admin/organizations/tenant-a/teams*', route => route.fulfill({ json: { items: [team, { id: 'design', name: 'Design team' }], has_more: false } }));
    await page.route('**/api/admin/organizations/tenant-a/users*', route => route.fulfill({ json: { items: [{ id: 'actor-a', name: 'Maya Chen', email: 'maya@example.test', github_username: 'maya' }], has_more: false } }));
    await page.route('**/api/admin/identity/organizations/tenant-a/github-members', route => {
      const body = route.request().postDataJSON();
      expect(body).toEqual({ github_username: 'octocat', team_id: 'design', role: 'member' });
      githubMember = body.github_username;
      return route.fulfill({ json: { id: 'new-person', github_username: githubMember } });
    });
    await page.route('**/api/v1/products/product-1/projects/project-1', route => {
      if (route.request().method() !== 'PATCH') return route.fallback();
      expect(route.request().postDataJSON()).toEqual({ team_id: 'design' });
      expect(route.request().headers()['if-profile-revision']).toBe('1');
      team = { id: 'design', name: 'Design team' }; revision++;
      return route.fulfill({ json: { ...api.profiles.get('product-1'), revision } });
    });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/products/product-1/projects/project-1');
    const people = page.getByRole('region', { name: 'People and access' });
    await expect(people.getByText('Platform team', { exact: true })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Purpose' })).toBeHidden();
    await page.screenshot({ path: testInfo.outputPath(`workspace-${width}.png`), fullPage: true });
    await people.getByRole('button', { name: 'Manage people' }).click();
    await people.getByRole('combobox', { name: 'Product team', exact: true }).selectOption('design');
    await people.getByRole('button', { name: 'Save people', exact: true }).click();
    await expect(people.getByText('Design team', { exact: true })).toBeVisible();
    await people.getByRole('button', { name: 'Manage people' }).click();
    await people.getByRole('combobox', { name: 'Add using', exact: true }).selectOption('github');
    await people.getByLabel('GitHub username', { exact: true }).fill('@octocat');
    await people.getByRole('button', { name: 'Add to team' }).click();
    await expect(people.getByText('New member (@octocat)')).toBeVisible();
    await page.getByRole('button', { name: 'Profile', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'Purpose' })).toBeVisible();
    await page.getByRole('textbox', { name: 'Purpose' }).fill('Unsaved purpose');
    await page.getByRole('button', { name: 'Profile', exact: true }).click();
    await page.getByRole('button', { name: 'Profile', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'Purpose' })).toHaveValue('Unsaved purpose');
    await page.screenshot({ path: testInfo.outputPath(`setup-${width}.png`), fullPage: true });
    const dimensions = await page.evaluate(() => ({ viewport: window.innerWidth, content: document.documentElement.scrollWidth }));
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
  });
}
