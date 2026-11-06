import { createHash } from 'node:crypto';
import { expect, test, type Page, type Route } from '@playwright/test';

// All provider interactions in this suite are fictional fixtures. An unmatched
// external request must never reach a real account during UI verification.
test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1'
    ? route.continue() : route.abort());
});

async function openPanel(page: Page, title: string) {
  const summary = page.locator('summary').filter({ hasText: new RegExp(`^${title}`) }).first();
  if (!await summary.evaluate(node => (node.parentElement as HTMLDetailsElement).open)) await summary.click();
}
async function section(page: Page, name: string) {
  await page.getByRole('navigation', { name: 'DevOps service sections' }).getByRole('link', { name, exact: true }).click();
}
async function closePanel(page: Page) {
  await page.getByRole('dialog').getByRole('button', { name: /^Close / }).click();
}
async function editSetup(page: Page) {
  await section(page, 'Setup');
  await page.getByRole('button', { name: 'Change setup' }).click();
}
async function manageWorkspace(page: Page) {
  await section(page, 'Setup');
  await page.getByRole('button', { name: 'Technical details', exact: true }).click();
  await page.getByRole('button', { name: 'Review workspace setup receipts' }).click();
}
async function refreshSetup(page: Page, discardDraft = false) {
  await section(page, 'Setup');
  if (discardDraft) page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Refresh setup' }).click();
}
async function selectWorkload(page: Page, _bindingId: string) {
  await manageWorkspace(page);
  await page.getByRole('button', { name: 'Review workspace access' }).click();
  await page.getByRole('button', { name: 'Check native UI access' }).click();
  await closePanel(page);
  await section(page, 'Runs & schedules');
}

const tenantId = 'tenant-fixture';
const productId = 'product-example';
const projects = ['project-river', 'project-lake'] as const;
const apiScope = (projectId: string) => `/api/v1/products/${productId}/projects/${projectId}`;
const devopsPath = (projectId: string) => `${apiScope(projectId)}/services/devops`;
const reference = (projectId: string, recordType: string, recordId: string) => ({
  tenant_id: tenantId, project_id: projectId, source_connection_id: 'connection-example', record_type: recordType,
  record_id: recordId, record_revision: '5',
});
const scope = (projectId: string) => ({ tenant_id: tenantId, product_id: productId, project_id: projectId,
  relationship_revision: '5', owner_id: 'actor-example', visibility: 'only_me' });
const settings = (projectId: string) => ({
  service_id: 'devops', profile_ref: reference(projectId, 'product', productId),
  capabilities: ['investigation'], enabled: false, visibility: 'only_me', exclusions: [],
  source_refs: [reference(projectId, 'environment', `environment-${projectId}`)],
  region: projectId === projects[0] ? 'us-east-1' : 'us-west-2',
  overrides: [{ field: 'region', value: projectId === projects[0] ? 'us-east-1' : 'us-west-2', reason: `Use ${projectId} workload` }], triggers: [],
  devops: { hosting_connection_ref: reference(projectId, 'connection', 'connection-example'),
    access_policy_ref: reference(projectId, 'policy', `policy-${projectId}`),
    region: projectId === projects[0] ? 'us-east-1' : 'us-west-2', space_mode: 'create', accounts: [], integrations: [],
    authentication_mode: 'scoped_iam_federation', permitted_native_actions: [],
    knowledge_policy: { source_refs: [], visibility: 'only_me', publish_results: false, refresh: 'manual' },
    trigger_policies: [], prevention_enabled: false, retention_days: 30, browser_launch_policy: 'disabled' },
});
type Configuration = ReturnType<typeof initialConfiguration>;
function initialConfiguration(projectId: string) {
  return { scope: scope(projectId), configuration_id: `config-${projectId}`, revision: 3,
    state: 'needs_input', handoff_state: 'pending', hash: `${projectId}-3`, inherited_fields: ['purpose', 'source_refs'],
    gaps: ['investigation.needs_space'], settings: settings(projectId) };
}

async function installFixture(page: Page) {
  const saved = new Map<string, Configuration>(projects.map(projectId => [projectId, initialConfiguration(projectId)]));
  const attempts: { projectId: string; etag: string | undefined; body: Record<string, unknown> }[] = [];
  const requests: { projectId: string; method: string; path: string }[] = [];
  let delayedProject: string | null = null;
  let release: (() => void) | null = null;

  await page.addInitScript((scopeTenant) => {
    const now = Math.floor(Date.now() / 1000);
    const token = `${btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))}.${btoa(JSON.stringify({
      sub: 'actor-example', email: 'owner@example.test', name: 'Example owner', auth_time: now,
      'custom:role': 'member', 'custom:org_id': scopeTenant, exp: now + 3600,
    }))}.${btoa('fixture-signature')}`;
    sessionStorage.setItem('cognito_access_token', token);
    sessionStorage.setItem('cognito_id_token', token);
    sessionStorage.setItem('cognito_token_expiry', String((now + 3600) * 1000));
  }, tenantId);

  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const projectId = projects.find(id => path.startsWith(apiScope(id))) ?? '';
    requests.push({ projectId, path, method });
    const respond = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers });
    if (path === '/api/features') return respond({ features: { product_profile: true } });
    if (path === '/api/access/status') return respond({ status: 'registered' });
    if (path === '/api/auth/workspaces') return respond({ items: [{ org_id: tenantId, name: 'Example organization' }] });
    if (path === '/api/v1/products') return respond({ items: [{ id: productId, name: 'Example delivery', revision: 5,
      base_project_id: projects[0], project_ids: [...projects], projects: projects.map(id => ({ id, name: id })) }], next_cursor: null });
    if (path === apiScope(projectId)) return respond({ tenant_id: tenantId, product_id: productId, project_id: projectId, revision: 5,
      product: { name: 'Example delivery', purpose: `Operate ${projectId}`, intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-example' } },
      project: { id: projectId, name: projectId, owner_user_id: 'actor-example' },
      configuration: { selected_capabilities: [{ capability: 'devops', ownership_mode: 'owned' }], advanced: { environments: [reference(projectId, 'environment', `environment-${projectId}`)] } } });
    if (path === `${apiScope(projectId)}/people`) return respond({ product_id: productId, revision: 5,
      owner: { id: 'actor-example', name: 'Example owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true,
      permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    if (path === `${devopsPath(projectId)}/configuration/options`) {
      if (delayedProject === projectId) {
        delayedProject = null;
        await new Promise<void>(resolve => { release = resolve; });
      }
      return respond({ connections: [Object.fromEntries(Object.entries(reference(projectId, 'connection', 'connection-example')).reverse())],
        sources: [reference(projectId, 'environment', `environment-${projectId}`)], regions: [],
        capabilities: ['investigation', 'release_readiness', 'release_verification', 'prevention'],
        unavailable_reasons: ['region_options_unavailable'] }).catch(() => {});
    }
    if (path === `${devopsPath(projectId)}/configuration` && method === 'GET') {
      const current = saved.get(projectId)!;
      return respond(current, 200, { ETag: `"${current.hash}"` });
    }
    if (path === `${devopsPath(projectId)}/configuration` && method === 'PUT') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      const etag = route.request().headers()['if-match'];
      attempts.push({ projectId, etag, body });
      const current = saved.get(projectId)!;
      if (etag !== `"${current.hash}"` || !route.request().headers()['idempotency-key'])
        return respond({ detail: { code: 'stale_revision' } }, 412);
      const next = { ...current, revision: current.revision + 1, hash: `${projectId}-${current.revision + 1}`,
        settings: { ...current.settings, ...body, devops: { ...current.settings.devops, ...(body.devops as object) } } };
      saved.set(projectId, next as Configuration);
      return respond(next, 200, { ETag: `"${next.hash}"` });
    }
    if ([`${devopsPath(projectId)}/bindings`, `${devopsPath(projectId)}/runs`, `${devopsPath(projectId)}/results`, `${devopsPath(projectId)}/delegations`].includes(path) && method === 'GET') return respond({ items: [] });
    return respond({ detail: { code: 'not_found' } }, 404);
  });

  return { saved, requests, attempts, delayOptions: (projectId: string) => { delayedProject = projectId; },
    releaseOptions: () => { release?.(); release = null; } };
}

test('test_ui_configuration: two project drafts and late responses stay isolated', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
  const fixture = await installFixture(page);
  await page.goto(`/products/${productId}/projects/${projects[0]}/services/devops`);
  await page.getByRole('button', { name: 'Technical details', exact: true }).click();
  await expect(page.getByText(/^region override: Use project-river workload/)).toBeVisible();
  await closePanel(page); await editSetup(page);
  await page.getByLabel('AWS Region').fill('us-east-2');
  await page.getByLabel('Reason for explicit Region or environment override').fill('Use River production region');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText(/Setup saved/)).toBeVisible();
  expect(fixture.attempts).toHaveLength(1);
  expect(fixture.attempts[0]).toMatchObject({ projectId: projects[0], etag: '"project-river-3"', body: { enabled: false, region: 'us-east-2', devops: { region: 'us-east-2' } } });
  fixture.delayOptions(projects[0]);
  const requested = page.waitForRequest(request => request.url().endsWith(`${devopsPath(projects[0])}/configuration/options`));
  await refreshSetup(page); await requested;
  await page.goto(`/products/${productId}/projects/${projects[1]}/services/devops`);
  await page.getByRole('button', { name: 'Technical details', exact: true }).click();
  await expect(page.getByText(/^region override: Use project-lake workload/)).toBeVisible();
  await closePanel(page); fixture.releaseOptions(); await editSetup(page);
  await expect(page.getByLabel('AWS Region')).toHaveValue('us-west-2');
  expect(fixture.saved.get(projects[0])?.settings.devops.region).toBe('us-east-2');
  expect(fixture.saved.get(projects[1])?.settings.devops.region).toBe('us-west-2');
  expect(fixture.requests.filter(request => request.method !== 'GET' && /binding-operations|\/runs/.test(request.path))).toHaveLength(0);
});

async function installOperationFixture(page: Page) {
  const fixture = await installFixture(page);
  const projectId = projects[0];
  const prefix = devopsPath(projectId);
  const bindingId = 'binding-example';
  const current = fixture.saved.get(projectId)!;
  fixture.saved.set(projectId, { ...current, state: 'ready', gaps: [], settings: {
    ...current.settings, enabled: true, capabilities: ['investigation', 'prevention'], devops: { ...current.settings.devops, browser_launch_policy: 'scoped_session',
      accounts: [{ account_id: '111122223333', environments: [{ environment_ref: reference(projectId, 'environment', 'development'), resource_refs: [reference(projectId, 'resource', 'workload-example')] }] }],
      trigger_policies: [{ kind: 'manual', enabled: true, capability: 'investigation', binding_id: bindingId,
        resource_refs: [reference(projectId, 'resource', 'workload-example')], telemetry_refs: [reference(projectId, 'telemetry', 'arn:aws:logs:us-east-1:111122223333:log-group:/aws/lambda/example-worker:*')] }] as never[] },
  } });
  const binding = { scope: scope(projectId), binding_id: bindingId, configuration_id: current.configuration_id,
    revision: 2, generation: 2, state: 'ready', ownership: 'adp_created', connection_ref: reference(projectId, 'connection', 'connection-example'),
    native_resource_ref: reference(projectId, 'agent_space', 'workspace-example'), provider_identity: { account_id: '111122223333', region: 'us-east-1' },
    gaps: [], readiness: ['investigation', 'native_ui', 'prevention'].map(capability => ({
      capability, state: 'ready', reason_codes: [], configuration_revision: 3, binding_generation: 2, valid_until: '2100-01-01T00:00:00Z',
    })) };
  const event = { event_id: 'event-example', scope: scope(projectId), binding_id: bindingId, origin: 'native_event',
    native_event_ref: reference(projectId, 'native_event', 'event-example'), received_at: '2026-10-27T11:00:00Z',
    state: 'reconciled', result_ref: reference(projectId, 'result', 'result-native') };
  const result = { scope: scope(projectId), result_id: 'result-native', run_id: 'run-native', revision: 1,
    observed_at: '2026-10-27T11:00:00Z', coverage: 'partial', coverage_mode: 'native_observation',
    native_job_ref: reference(projectId, 'native_job', 'job-native'), origin: 'native', provider_outcome: 'outcome_unknown',
    publication_id: 'publication-example' };
  const run = { scope: scope(projectId), run_id: 'run-manual', revision: 1, state: 'admitted',
    provider_state: 'pending', deadline_at: '2026-10-27T13:00:00Z', origin: 'adp_submitted' };
  let submitted = false;
  let prevention = { binding_id: bindingId, configuration_revision: 3, desired: { enabled: true }, observed: null,
    status: 'blocked', reason: 'prevention_readback_missing' };
  const mutations: { path: string; headers: Record<string, string>; body: Record<string, unknown> }[] = [];

  await page.route(`**${prefix}/**`, async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const respond = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers });
    if (method === 'GET' && path === `${prefix}/bindings`) return respond({ items: [binding] });
    if (method === 'GET' && path === `${prefix}/bindings/${bindingId}`) return respond(binding, 200, { ETag: '"binding-two"' });
    if (method === 'GET' && path === `${prefix}/bindings/${bindingId}/prevention`) return respond(prevention);
    if (method === 'GET' && path === `${prefix}/bindings/${bindingId}/events`) return respond({ items: [event] });
    if (method === 'GET' && path === `${prefix}/runs`) return respond({ items: submitted ? [run] : [] });
    if (method === 'GET' && path === `${prefix}/runs/run-manual`) return respond(run, 200, { ETag: '"run-one"' });
    if (method === 'GET' && path === `${prefix}/results`) return respond({ items: [result] });
    if (method === 'GET' && path === `${prefix}/results/result-native/findings`) return respond({ items: [{
      finding_id: 'finding-example', claim: 'Investigate the error trend', validation: 'candidate', source_refs: [],
    }] });
    if (method === 'GET' && path === `${prefix}/publications/publication-example`) return respond({
      publication_id: 'publication-example', result_id: 'result-native', state: 'retry_wait', invalidation_watermark: 0,
    });
    if (method === 'GET' && path === `${prefix}/binding-operations/operation-example`) return respond({
      operation_id: 'operation-example', state: 'waiting', revision: 1, action: 'check', gaps: ['provider_readback_pending'],
    });
    if (method === 'POST') {
      const headers = route.request().headers();
      const body = route.request().postDataJSON() as Record<string, unknown>;
      mutations.push({ path, headers, body });
      if (!headers.authorization?.startsWith('Bearer ')) return respond({ detail: { code: 'session_required' } }, 401);
      if (path === `${prefix}/configuration/check` || path === `${prefix}/bindings/${bindingId}/browser-launch/check`) return respond({
        ready: true, gaps: [], scope: scope(projectId), capability: path.endsWith('/browser-launch/check') ? 'native_ui' : 'investigation',
        configuration_id: current.configuration_id, configuration_revision: 3, configuration_hash: current.hash,
        binding_id: bindingId, binding_generation: 2, binding_revision: 2,
        checked_at: '2026-10-27T12:00:00Z', valid_until: '2026-10-27T12:01:00Z',
      });
      if (path === `${prefix}/binding-operations`) return respond({ operation_id: 'operation-example', state: 'accepted', revision: 1, action: 'check', gaps: [] }, 202, { ETag: '"operation-one"' });
      if (path === `${prefix}/runs`) {
        if (body.service_id !== 'devops' || body.configuration_revision !== 3 || body.binding_id !== bindingId) return respond({ detail: { code: 'invalid_input' } }, 422);
        submitted = true;
        return respond(run, 202, { ETag: '"run-one"' });
      }
      if (path === `${prefix}/bindings/${bindingId}/prevention`) {
        prevention = { ...prevention, desired: { enabled: false }, reason: 'prevention_change_pending' };
        return respond(prevention, 202);
      }
      if (path === `${prefix}/bindings/${bindingId}/browser-launch`) return respond({ binding_id: bindingId,
        launch_url: 'https://signin.aws.amazon.com/federation?Action=login&SigninToken=fixture-ticket',
        expires_at: '2026-10-27T12:15:00Z' });
    }
    return route.fallback();
  });
  return { ...fixture, prefix, bindingId, mutations, run, result, event, currentPrevention: () => prevention };
}

test('simple workspace keeps setup compact and preserves panel drafts on desktop and mobile', async ({ page }, testInfo) => {
  const fixture = await installOperationFixture(page);
  await page.goto(`/products/${productId}/projects/${projects[0]}/services/devops`);
  const workspace = page.getByRole('region', { name: 'DevOps configuration' });
  const setup = page.getByRole('region', { name: 'Setup', exact: true });
  for (const name of ['Workload', 'AWS connection', 'DevOps workspace']) await expect(setup.getByRole('region', { name, exact: true })).toBeVisible();
  await expect(setup.getByText('From Product profile', { exact: true })).toBeVisible();
  await expect(setup.getByText('Service override', { exact: true })).toBeVisible();
  await expect(setup.getByRole('textbox')).toHaveCount(0);
  await expect(workspace.getByRole('button', { name: 'Save changes' })).toHaveCount(0);
  await expect(workspace.getByRole('button', { name: /Read retained setup receipt|Read owner consent/ })).toHaveCount(0);
  const capture = async (name: string) => {
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth));
    await expect.poll(() => page.evaluate(async () => {
      window.scrollTo({ top: 0, behavior: 'instant' });
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      return window.scrollY;
    })).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
  };
  await capture('setup-desktop');
  await editSetup(page);
  await expect(page.getByLabel('Hosting AWS connection')).toHaveValue('0');
  await expect(page.getByLabel('AWS Region')).toBeVisible();
  await page.getByLabel('AWS Region').fill('us-west-2');
  await page.getByLabel('Reason for explicit Region or environment override').fill('Draft region change');
  await page.screenshot({ path: testInfo.outputPath('edit-desktop.png'), fullPage: false });
  // Native modal confines keyboard focus and Escape returns to its trigger.
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => Boolean(document.activeElement?.closest('dialog[open]')))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Change setup' })).toBeFocused();
  await editSetup(page); await expect(page.getByLabel('AWS Region')).toHaveValue('us-west-2'); await closePanel(page);
  await section(page, 'Runs & schedules');
  await expect(page.getByRole('heading', { name: 'Start an investigation' })).toBeVisible();
  await expect(workspace.getByLabel('Investigation question')).toBeVisible();
  await workspace.getByLabel('Investigation question').fill('Why did the application error rate increase?');
  await expect(workspace.getByRole('button', { name: 'Start investigation' })).toBeEnabled();
  await capture('runs-desktop');
  await page.setViewportSize({ width: 390, height: 844 });
  await capture('runs-mobile');
  await section(page, 'Setup'); await capture('setup-mobile');
  await editSetup(page); await expect(page.getByLabel('AWS Region')).toHaveValue('us-west-2');
  await page.screenshot({ path: testInfo.outputPath('edit-mobile.png'), fullPage: false }); await closePanel(page);
  await section(page, 'Results');
  await workspace.getByRole('button', { name: 'View result' }).click();
  await expect(workspace.getByText(/Investigate the error trend: candidate/)).toBeVisible();
  await capture('results-mobile');
  expect(fixture.mutations).toEqual([]);
});

test('test_ui_operation: scoped binding, run, prevention, native work and browser handoff', async ({ page }, testInfo) => {
  await page.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
  const fixture = await installOperationFixture(page);
  await page.goto(`/products/${productId}/projects/${projects[0]}/services/devops`);
  const operations = page.getByRole('region', { name: 'DevOps configuration' });
  await expect(operations.getByRole('button', { name: 'Open workspace', exact: true })).toBeVisible();
  await selectWorkload(page, fixture.bindingId);
  await section(page, 'Results');
  await openPanel(page, 'Native events and evidence');
  await expect(operations.getByText(/event-example: reconciled/)).toBeVisible();
  await section(page, 'Runs & schedules');
  await openPanel(page, 'Prevention schedule');
  await expect(operations.getByText(/Prevention: blocked; desired: enabled; observed: not observed/)).toBeVisible();
  await section(page, 'Setup');
  await expect(operations.getByRole('button', { name: 'Open workspace', exact: true })).toBeEnabled();
  await section(page, 'Setup');
  await manageWorkspace(page);
  await operations.getByRole('button', { name: 'Check binding' }).click();
  await expect(operations.getByRole('status').filter({ hasText: /^Binding operation operation-example: accepted/ })).toBeVisible();
  await operations.getByRole('button', { name: 'Check operation' }).click();
  await expect(operations.getByText(/Binding operation operation-example: waiting/)).toBeVisible();
  await closePanel(page);
  await refreshSetup(page);
  await selectWorkload(page, fixture.bindingId);
  await operations.getByLabel('Investigation question').fill('Investigate selected resource');
  await operations.getByRole('button', { name: 'Start investigation' }).click();
  await expect(operations.getByRole('status').filter({ hasText: /^Run run-manual: admitted/ })).toBeVisible();
  await expect(operations.getByRole('listitem', { name: /Run run-manual: admitted; origin adp_submitted/ })).toBeVisible();
  await section(page, 'Results');
  await operations.getByRole('button', { name: 'View result' }).click();
  await expect(operations.getByText(/Investigate the error trend: candidate/)).toBeVisible();
  await expect(operations.getByText(/Context publication publication-example: retry_wait/)).toBeVisible();
  await section(page, 'Runs & schedules');
  await openPanel(page, 'Prevention schedule');
  await operations.getByRole('button', { name: 'pause prevention' }).click();
  await expect(operations.getByText(/Prevention pause requested/)).toBeVisible();
  expect(fixture.currentPrevention().reason).toBe('prevention_change_pending');
  await page.screenshot({ path: process.env.ADP_DEVOPS_SCREENSHOT_DIR ? process.env.ADP_DEVOPS_SCREENSHOT_DIR + '/devops-desktop.png' : testInfo.outputPath('devops-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: process.env.ADP_DEVOPS_SCREENSHOT_DIR ? process.env.ADP_DEVOPS_SCREENSHOT_DIR + '/devops-mobile.png' : testInfo.outputPath('devops-mobile.png'), fullPage: true });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth));
  await page.setViewportSize({ width: 1280, height: 900 });
  const check = fixture.mutations.find(item => item.path.endsWith('/binding-operations'))!;
  const manual = fixture.mutations.find(item => item.path.endsWith('/runs'))!;
  const paused = fixture.mutations.find(item => item.path.endsWith('/prevention'))!;
  expect(check.headers).toMatchObject({ 'if-match': '"binding-two"', 'idempotency-key': expect.any(String) });
  expect(manual.body).toMatchObject({ service_id: 'devops', configuration_revision: 3, binding_id: fixture.bindingId,
    devops_input: { kind: 'investigation', question: 'Investigate selected resource' } });
  expect(paused).toMatchObject({ headers: { 'if-match': '"binding-two"', 'idempotency-key': expect.any(String) }, body: { action: 'pause', configuration_revision: 3 } });
  const readback = await page.evaluate(async path => ({ run: await (await fetch(`${path}/runs/run-manual`)).json(),
    events: await (await fetch(`${path}/bindings/binding-example/events`)).json() }), fixture.prefix);
  expect(readback).toMatchObject({ run: { run_id: 'run-manual', scope: { project_id: projects[0] } },
    events: { items: [{ origin: 'native_event', result_ref: { record_id: 'result-native' } }] } });
  await refreshSetup(page);
  await selectWorkload(page, fixture.bindingId);
  await section(page, 'Setup');
  await expect(operations.getByRole('button', { name: 'Open workspace', exact: true })).toBeEnabled();
  await page.route('https://signin.aws.amazon.com/federation?**', route => route.fulfill({ status: 200,
    contentType: 'text/html', body: '<main>Fictional native handoff complete</main>' }));
  await section(page, 'Setup');
  await operations.getByRole('button', { name: 'Open workspace', exact: true }).click();
  await expect(page.getByText('Fictional native handoff complete')).toBeVisible();
  expect(fixture.mutations.filter(item => item.path.endsWith('/browser-launch'))).toHaveLength(1);
  expect(fixture.mutations.at(-1)?.body).toEqual({ configuration_revision: 3 });
});

async function installFailureFixture(page: Page) {
  const fixture = await installOperationFixture(page);
  let saveFault: 412 | 503 | null = null;
  let readFault: 403 | null = null;
  const saves: { etag: string | undefined; region: string }[] = [];
  let launchAttempts = 0;
  await page.route(`**${fixture.prefix}/configuration`, async (route: Route) => {
    const method = route.request().method();
    if (method === 'GET' && readFault) return route.fulfill({ status: readFault, contentType: 'application/json',
      body: JSON.stringify({ detail: { code: 'access_denied', message: 'fixture-ticket' } }) });
    if (method === 'PUT' && saveFault) {
      saves.push({ etag: route.request().headers()['if-match'], region: route.request().postDataJSON().devops.region });
      return route.fulfill({ status: saveFault, contentType: 'application/json',
        body: JSON.stringify({ detail: { code: saveFault === 412 ? 'stale_revision' : 'readback_unavailable', message: 'fixture-ticket' } }) });
    }
    return route.fallback();
  });
  await page.route(`**${fixture.prefix}/bindings/${fixture.bindingId}/browser-launch`, async (route: Route) => {
    launchAttempts += 1;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      binding_id: fixture.bindingId,
      launch_url: 'https://signin.aws.amazon.com/federation?Action=login&SigninToken=fixture-ticket',
      expires_at: '2026-10-27T11:00:00Z',
    }) });
  });
  await page.route('https://signin.aws.amazon.com/federation?**', route => route.fulfill({ status: 200,
    contentType: 'text/html', body: '<main>Unexpected native navigation</main>' }));
  return { ...fixture, saves, setSaveFault: (fault: 412 | 503 | null) => { saveFault = fault; },
    setReadFault: (fault: 403 | null) => { readFault = fault; }, launchAttempts: () => launchAttempts };
}

test('test_ui_failure_states: failed saves remain recoverable and denied access clears the product', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
  const fixture = await installFailureFixture(page);
  await page.goto(`/products/${productId}/projects/${projects[0]}/services/devops`);
  await selectWorkload(page, fixture.bindingId); await section(page, 'Setup');
  await page.getByRole('button', { name: 'Open workspace', exact: true }).click();
  await expect(page.getByRole('region', { name: 'DevOps workspace', exact: true })).toContainText('Native UI access could not be confirmed');
  expect(fixture.launchAttempts()).toBe(1);
  await expect(page.locator('body')).not.toContainText('fixture-ticket');
  fixture.setSaveFault(412); await editSetup(page);
  await page.getByLabel('AWS Region').fill('us-east-2');
  await page.getByLabel('Reason for explicit Region or environment override').fill('Keep River workload close');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('changed elsewhere');
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  await closePanel(page);
  page.once('dialog', dialog => dialog.dismiss()); await page.getByRole('button', { name: 'Refresh setup' }).click();
  await editSetup(page); await expect(page.getByLabel('AWS Region')).toHaveValue('us-east-2'); await closePanel(page);
  await refreshSetup(page, true); await editSetup(page); await expect(page.getByLabel('AWS Region')).toHaveValue('us-east-1');
  fixture.setSaveFault(503); await page.getByLabel('AWS Region').fill('us-west-1');
  await page.getByLabel('Reason for explicit Region or environment override').fill('Retain this draft');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('could not be confirmed');
  await closePanel(page); await editSetup(page); await expect(page.getByLabel('AWS Region')).toHaveValue('us-west-1'); await closePanel(page);
  expect(fixture.saves).toHaveLength(2);
  fixture.setReadFault(403); await refreshSetup(page, true);
  await expect(page.getByRole('alert')).toContainText('access is unavailable');
  await expect(page.getByText('workload-example', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Manage access' })).toBeDisabled();
});

test('reads a compressed retained report and presents its summary without weakening evidence verification', async ({ page }, testInfo) => {
  const fixture = await installOperationFixture(page);
  const native = reference(projects[0], 'devops_execution', 'execution-example');
  const evidence = { kind: 'devops_investigation', native_job_ref: native,
    task: { taskId: 'task-example', executionId: 'execution-example', status: 'COMPLETED', description: JSON.stringify({ question: 'Why are requests timing out?' }) },
    journal_records: [
      { recordType: 'investigation_summary_md', content: '## Connection timeout\n\nRequests timed out while waiting for the downstream application.' },
      { recordType: 'mitigation_summary_md', content: 'Compare the timeout settings with the last successful deployment.' },
    ], operational_recommendations: [], padding: 'retained observation '.repeat(100),
  };
  const bytes = JSON.stringify(evidence);
  const artifact = { artifact_id: 'readback-result-example', version: '1', media_type: 'application/json', bytes: Buffer.byteLength(bytes), sha256: createHash('sha256').update(bytes).digest('hex') };
  const result = { ...fixture.result, result_id: 'result-example', service_id: 'devops', publication_id: undefined,
    origin: 'adp_submitted', native_task_id: 'task-example', native_job_ref: native, evidence_ref: artifact };
  let corrupt = true;
  const requests: string[] = [];
  await page.route(`**${fixture.prefix}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    const respond = (body: unknown, headers = {}) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body), headers });
    if (path === `${fixture.prefix}/results`) return respond({ items: [result] });
    if (path === `${fixture.prefix}/results/result-example/findings`) return respond({ items: [] });
    if (path === `${fixture.prefix}/results/result-example`) {
      requests.push(path);
      return respond(result, { ETag: 'W/"1"' }); // CloudFront weakens the tag after compression.
    }
    if (path === `${fixture.prefix}/artifacts/${artifact.artifact_id}`) return respond({ artifact,
      download_path: `${fixture.prefix.replace('/api', '')}/artifacts/${artifact.artifact_id}/content`, expires_at: new Date(Date.now() + 60000).toISOString() });
    if (path === `${fixture.prefix}/artifacts/${artifact.artifact_id}/content`) return route.fulfill({ contentType: 'application/json',
      body: corrupt ? bytes.replace('downstream', 'unexpected') : bytes, headers: { 'X-Adp-Content-Sha256': artifact.sha256 } });
    return route.fallback();
  });
  await page.goto(`/products/${productId}/projects/${projects[0]}/services/devops`);
  await section(page, 'Results');
  await page.getByRole('button', { name: 'View result' }).click();
  await page.getByRole('button', { name: 'Read investigation report' }).click();
  await expect(page.getByRole('alert')).toContainText('could not be read and verified');
  await expect(page.getByRole('heading', { name: 'Connection timeout' })).toHaveCount(0);
  corrupt = false;
  await page.getByRole('button', { name: 'Read investigation report' }).click();
  await expect(page.getByRole('heading', { name: 'Connection timeout' })).toBeVisible();
  await expect(page.getByText('Compare the timeout settings with the last successful deployment.', { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(requests).toHaveLength(2);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.locator('body')).toHaveJSProperty('scrollWidth', await page.locator('body').evaluate(node => node.clientWidth));
    await expect.poll(() => page.evaluate(async () => {
      window.scrollTo({ top: 0, behavior: 'instant' });
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      return window.scrollY;
    })).toBe(0);
    await page.screenshot({ path: testInfo.outputPath(`report-${width}.png`), fullPage: true });
  }
});

test('makes SSO visible and explains missing access without opening any provider page', async ({ page }) => {
  const fixture = await installOperationFixture(page);
  const saved = fixture.saved.get(projects[0])!;
  fixture.saved.set(projects[0], { ...saved, settings: { ...saved.settings, devops: { ...saved.settings.devops, browser_launch_policy: 'disabled' } } });
  await page.route(`**${fixture.prefix}/bindings/${fixture.bindingId}/browser-launch/check`, route => route.fulfill({
    status: 404, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'not_found' } }),
  }));
  await page.goto(`/products/${productId}/projects/${projects[0]}/services/devops`);
  await page.getByRole('button', { name: 'Set up sign-in' }).click();
  const signIn = page.getByRole('complementary', { name: 'AWS DevOps Agent sign-in' });
  await expect(signIn.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeVisible();
  await expect(signIn.getByText(/Workspace sign-in is not configured/)).toBeVisible();
  await signIn.getByRole('button', { name: 'Review workspace access' }).click();
  await signIn.getByRole('button', { name: 'Check native UI access' }).click();
  await expect(signIn.getByRole('alert')).toContainText('enable its AWS operator app');
  await expect(signIn.getByRole('button', { name: 'Open AWS DevOps Agent' })).toBeDisabled();
  await expect(page).toHaveURL(/127.0.0.1/);
  expect(fixture.mutations.some(item => item.path.endsWith('/browser-launch'))).toBe(false);
});
