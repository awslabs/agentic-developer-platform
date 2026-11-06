import { test, expect, type Page } from '@playwright/test';

const root = '/products/product-one/projects/project-one';
const api = `/api/v1${root}`;
const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5', owner_id: 'owner-one' };
const ref = (record_type: string, record_id: string) => ({ tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type, record_id, record_revision: '17' });
const document = ref('document', 'security-design');
const repository = ref('repository_commit', 'full-repository');
const connection = ref('connection', 'connection-one');

async function install(page: Page) {
  let budget = 500;
  let buildEnabled = false;
  const agentRun = { invocation_id: 'delivery-run', invoked_at: '2026-11-03T10:00:00Z', persona: 'operations', channel: 'github', status: 'in_progress', topic: 'Deploy checkout safely', transcript_key: 'retained/delivery.md', correlation_id: 'delivery-chain', viewer_is_run_owner: false };
  const errors: string[] = [];
  const external: string[] = [];
  const writes: Array<{ path: string; method: string; body: Record<string, unknown> }> = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.context().route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin === 'http://127.0.0.1:5193') return route.continue();
    external.push(url.origin); return route.abort();
  });
  await page.addInitScript(() => {
    const now = Math.floor(Date.now() / 1000);
    const token = `${btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))}.${btoa(JSON.stringify({ sub: 'owner-one', email: 'owner@example.test', name: 'Example owner', auth_time: now, 'custom:role': 'member', 'custom:org_id': 'tenant-one', exp: now + 3600 }))}.${btoa('test-signature')}`;
    sessionStorage.setItem('cognito_access_token', token); sessionStorage.setItem('cognito_id_token', token); sessionStorage.setItem('cognito_token_expiry', String((now + 3600) * 1000));
  });
  const registration = { kind: 'code_review_source_registration', registration: { service_role: 'arn:aws:iam::111122223333:role/security-assessment', bucket: 'fixture-sources' }, observed_at: new Date().toISOString() };
  const binding = { scope, binding_id: 'binding-one', configuration_id: 'security-one', revision: 2, generation: 3, state: 'ready', ownership: 'adp_created', connection_ref: connection, provider_identity: { account_id: '111122223333' }, native_resource_ref: ref('security_space', 'space-one'), gaps: [], readiness: [registration, { kind: 'threat_model', native_model_ref: ref('threat_model', 'model-one'), observed_at: new Date().toISOString() }] };
  let security = { scope, configuration_id: 'security-one', revision: 4, hash: 'security-four', state: 'ready', handoff_state: 'linked', inherited_fields: ['purpose', 'source_refs', 'classification'], gaps: [], settings: {
    purpose: 'Secure checkout', classification: 'Confidential', profile_ref: ref('product', 'product-one'), enabled: true, capabilities: ['threat_model', 'code_review'], source_refs: [document, repository], connection_ref: connection, region: 'us-east-1', visibility: 'only_me', exclusions: [], overrides: [], triggers: [
      { trigger_id: 'manual-one', kind: 'manual', enabled: true, capability: 'threat_model', binding_id: 'binding-one', source_refs: [document], debounce_seconds: 0 },
      { trigger_id: 'nightly-code', kind: 'schedule', enabled: true, capability: 'code_review', binding_id: 'binding-one', source_refs: [repository], debounce_seconds: 0, schedule: '0 2 * * *' },
    ],
  } };
  let run: Record<string, unknown> | null = null;
  let denyEvidence = false;
  let devopsReady = false;
  const environment = ref('environment', 'staging');
  const resource = ref('aws_resource', 'checkout-function');
  const devops = { scope, configuration_id: 'devops-one', revision: 4, hash: 'devops-four', state: 'ready', handoff_state: 'linked', inherited_fields: ['purpose', 'source_refs'], gaps: [], settings: {
    service_id: 'devops', purpose: 'Operate checkout', profile_ref: ref('product', 'product-one'), source_refs: [environment], capabilities: ['investigation'], enabled: true, visibility: 'only_me', exclusions: [], overrides: [], triggers: [],
    devops: { hosting_connection_ref: connection, access_policy_ref: connection, region: 'us-east-1', space_mode: 'create', accounts: [], integrations: [], authentication_mode: 'scoped_iam_federation', permitted_native_actions: [], prevention_enabled: false, retention_days: 30, browser_launch_policy: 'disabled',
      knowledge_policy: { source_refs: [], visibility: 'only_me', publish_results: false, refresh: 'manual' },
      trigger_policies: [{ trigger_id: 'manual-devops', kind: 'manual', enabled: true, capability: 'investigation', binding_id: 'devops-binding', resource_refs: [resource], telemetry_refs: [] }] },
  } };
  const devopsBinding = { ...binding, binding_id: 'devops-binding', configuration_id: 'devops-one', readiness: [] };
  let devopsRun: Record<string, unknown> | null = null;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname, method = route.request().method();
    const send = (body: unknown, status = 200, headers = {}) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers });
    if (method !== 'GET') writes.push({ path, method, body: route.request().postDataJSON() ?? {} });
    if (path === '/api/features') return send({ features: { product_profile: true, orchestration_engine: buildEnabled, agent_explanations: true } });
    if (path === '/api/v1/projects/project-one/budget') {
      if (method === 'PUT') budget = Number(route.request().postDataJSON().budget_usd);
      return send({ product_id: 'product-one', project_id: 'project-one', project_name: 'General', is_base: true, budget_usd: budget, spent_usd: 40, reserved_usd: 10, available_usd: budget - 50, accounting_status: 'current', can_manage: true, flows: [] });
    }
    if (path === `${api}/agent-invocations`) return send({ items: buildEnabled ? [agentRun] : [], count: buildEnabled ? 1 : 0, last_key: null });
    if (path === `${api}/agent-invocations/delivery-run`) return send(agentRun);
    if (path === `${api}/agent-invocations/delivery-run/transcript`) return route.fulfill({ status: 200, contentType: 'text/markdown', body: '## Run results\nDeployment checks completed for the selected project.' });
    if (path === `${api}/agent-invocations/delivery-run/events`) {
      const timestamp = new Date().toISOString();
      const event = { version: 1, invocation_id: 'delivery-run', generation: 1, sequence: 1, timestamp, kind: 'explanation', payload: { text: '- ☑ `test` prepare — Check configuration\n- ☐ `infra` deploy — Deploy service', progress: { id: 'board', category: 'plan', state: 'running', started_at: timestamp } } };
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: delivery-run:1:1\nevent: explanation\ndata: ${JSON.stringify(event)}\n\nevent: finished\ndata: {}\n\n` });
    }
    if (path === '/api/orchestration/flows') {
      expect(url.searchParams.get('product_id')).toBe('product-one');
      expect(url.searchParams.get('project_id')).toBe('project-one');
      return send({ product_id: 'product-one', project_id: 'project-one', flows: [], total: 0, limit: 25, offset: 0, status_counts: { running: 0, complete: 0, awaiting_you: 0, attention_needed: 0, queued: 0, empty: 0, archived: 0 } });
    }
    if (path === '/api/access/status') return send({ status: 'registered' });
    if (path === '/api/auth/workspaces') return send({ items: [{ org_id: 'tenant-one', name: 'Example organization' }] });
    if (path === '/api/v1/products') return send({ items: [{ id: 'product-one', name: 'Checkout', revision: 5, project_ids: ['project-one'], projects: [{ id: 'project-one', name: 'General' }] }], next_cursor: null });
    if (path === api) return send({ ...scope, revision: 5, configuration: {}, product: { name: 'Checkout', purpose: 'Reliable checkout', intended_users: ['Customers'], owner_ref: { kind: 'user', id: 'owner-one' } }, project: { id: 'project-one', name: 'General', owner_user_id: 'owner-one' } });
    if (path === `${api}/people`) return send({ product_id: 'product-one', revision: 5, owner: { id: 'owner-one', name: 'Example owner' }, team: null, members: [], next_offset: null, is_owner: true, permissions: {} });
    if (path === `${api}/bindings`) return send({ items: [{ id: 'source-one', project_id: 'project-one', status: 'healthy', selection: { kind: 'repository', provider: 'github', namespace: 'team', native_id: 'checkout' } }] });
    if (path === `${api}/sources`) return send({ items: [], next_cursor: null });
    if (path === '/api/engineering-insights/v1/projects/project-one/results') return denyEvidence ? send({ error: { code: 'projection_incomplete' } }, 409) : send({ schema_version: 'results/1.0', view: 'business_context', product_id: 'product-one', project_id: 'project-one', as_of: url.searchParams.get('as_of'), dataset_version: 'dataset-fixture', mapping_versions: ['mapping-fixture'], coverage: 'complete', work_count: 5, cost_by_currency: { USD: '18.50' }, contexts: [], next_cursor: null });
    if (path === `${api}/services/security/configuration/options`) return send({ connections: [connection], sources: [document, repository], regions: [], capabilities: ['threat_model', 'code_review'], unavailable_reasons: [] });
    if (path === `${api}/services/security/configuration`) {
      if (method === 'PUT') {
        const body = route.request().postDataJSON();
        expect(route.request().headers()['if-match']).toBe(`"${security.hash}"`);
        expect(route.request().headers()['idempotency-key']).toBeTruthy();
        security = { ...security, revision: security.revision + 1, hash: `security-${security.revision + 1}`, settings: { ...security.settings, ...body } };
      }
      return send(security, 200, { ETag: `"${security.hash}"` });
    }
    if (path === `${api}/services/security/configuration/check`) return send({ ready: true, gaps: [], provider_contacted: false });
    if (path === `${api}/services/security/bindings`) return send({ items: [binding] });
    if (path === `${api}/services/security/bindings/binding-one`) return send(binding, 200, { ETag: '"binding-two"' });
    if (path === `${api}/services/security/runs`) {
      if (method === 'POST') { run = { scope, run_id: 'run-one', revision: 1, state: 'complete', provider_state: 'completed', deadline_at: '2026-11-03T00:00:00Z', monitoring: { state: 'fresh', poll_attempts: 1 }, input: route.request().postDataJSON() }; return send(run, 201, { ETag: '"run-one"' }); }
      return send({ items: run ? [run] : [] });
    }
    if (path === `${api}/services/security/runs/run-one`) return send(run, 200, { ETag: '"run-one"' });
    if (path === `${api}/services/security/results`) return send({ items: [] });
    if (path === `${api}/services/devops/configuration/options`) return send({ connections: [connection], sources: [environment], regions: [], capabilities: ['investigation'], unavailable_reasons: [] });
    if (path === `${api}/services/devops/configuration`) return send(devops, 200, { ETag: '"devops-four"' });
    if (path === `${api}/services/devops/configuration/check`) return send({ ready: devopsReady, gaps: devopsReady ? [] : ['execution_consent_required'], scope, capability: 'investigation', configuration_id: 'devops-one', configuration_revision: 4, configuration_hash: 'devops-four', binding_id: 'devops-binding', binding_generation: 3, binding_revision: 2, checked_at: new Date(Date.now() - 1000).toISOString(), valid_until: new Date(Date.now() + 59_000).toISOString() });
    if (path === `${api}/services/devops/bindings`) return send({ items: [devopsBinding] });
    if (path === `${api}/services/devops/bindings/devops-binding`) return send(devopsBinding, 200, { ETag: '"binding-two"' });
    if (path === `${api}/services/devops/bindings/devops-binding/events`) return send({ items: [] });
    if (path === `${api}/services/devops/bindings/devops-binding/prevention`) return send({ binding_id: 'devops-binding', configuration_revision: 4, status: 'blocked', reason: 'not_enabled', desired: { enabled: false }, observed: null });
    if (path === `${api}/services/devops/bindings/devops-binding/browser-launch/check`) return send({ detail: { code: 'native_ui_consent_required' } }, 403);
    if (path === `${api}/services/devops/runs`) {
      if (method === 'POST') { devopsRun = { scope, run_id: 'investigation-one', revision: 1, state: 'complete', provider_state: 'completed', deadline_at: '2026-11-03T00:00:00Z', origin: 'adp_submitted' }; return send(devopsRun, 201, { ETag: '"run-one"' }); }
      return send({ items: devopsRun ? [devopsRun] : [] });
    }
    if (path === `${api}/services/devops/results`) return send({ items: [{ scope, result_id: 'devops-result', run_id: 'historic-run', revision: 1, observed_at: '2026-11-02T00:00:00Z', coverage: 'partial', coverage_mode: 'native_observation', native_job_ref: resource, provider_outcome: 'outcome_unknown', origin: 'native' }] });
    if (path === `${api}/services/devops/results/devops-result/findings`) return send({ items: [{ finding_id: 'finding-one', claim: 'Inspect dependency retries', validation: 'candidate', source_refs: [] }] });
    if (path.endsWith('/configuration/options')) return send({ connections: [connection], sources: [], regions: [], capabilities: path.includes('/devops/') ? ['investigation'] : [], admissions_enabled: false, unavailable_reasons: [] });
    if (path.endsWith('/configuration') || path.endsWith('/gitlab')) return send({ detail: { code: 'not_found' } }, 404);
    if (/\/(bindings|runs|results)$/.test(path)) return send({ items: [] });
    return send({ detail: { code: 'not_found' } }, 404);
  });
  return { errors, external, writes, enableBuild: () => { buildEnabled = true; }, security: () => security, denyEvidence: () => { denyEvidence = true; }, allowDevops: () => { devopsReady = true; },
    enableCodeScan: () => { security.settings.triggers[0] = { ...security.settings.triggers[0], capability: 'code_review', source_refs: [repository] }; } };
}

for (const width of [1440, 1024, 768, 390, 320]) test(`product workspaces remain usable at ${width}px`, async ({ page }, info) => {
  const fixture = await install(page); await page.setViewportSize({ width, height: 1000 }); await page.goto(root);
  const nav = page.getByRole('navigation', { name: 'Product workspace', exact: true });
  await expect(nav.getByRole('button')).toHaveText(['Overview', 'Build', 'Release', 'Operate', 'Insights', 'Agents', 'Context', 'Budget & spend']);
  await nav.getByRole('button', { name: 'Release', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Prepare', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open code scanning' })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'Product summary' })).toBeHidden();
  await nav.getByRole('button', { name: 'Agents', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Product agents' }).getByRole('link')).toHaveCount(5);
  await page.getByRole('link', { name: 'Open Security', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Product services', exact: true })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: 'Security service sections' }).getByRole('link')).toHaveText(['Setup', 'Runs & schedules', 'Results']);
  await expect(page.getByRole('region', { name: 'From Product profile' }).getByText('Secure checkout')).toBeVisible();
  await nav.getByRole('button', { name: 'Build', exact: true }).click();
  await page.getByRole('tab', { name: 'Delivery flows' }).focus(); await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Agent activity' })).toBeFocused();
  await page.reload(); await expect(page.getByRole('tab', { name: 'Agent activity' })).toHaveAttribute('aria-selected', 'true');
  await nav.getByRole('button', { name: 'Insights', exact: true }).click();
  await expect(page.getByRole('button', { name: 'View report' })).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath(`insights-${width}.png`), fullPage: true });
  expect(fixture.errors).toEqual([]); expect(fixture.external).toEqual([]); expect(fixture.writes).toEqual([]);
});

test('Security setup, nightly schedule, manual assessment and retained results use the original contracts', async ({ page }) => {
  const fixture = await install(page); await page.goto(`${root}/release/security?capability=threat_model#l5-security-runs`);
  const assessments = page.getByRole('region', { name: 'Security assessments' });
  await expect(assessments.getByLabel('Assessment capability')).toHaveValue('threat_model');
  await expect(assessments.getByRole('button', { name: 'Start Security assessment' })).toBeDisabled();
  await assessments.getByLabel('Assessment binding').selectOption('binding-one');
  await assessments.getByLabel('security-design (revision 17)').check();
  await assessments.getByRole('button', { name: 'Check Security readiness' }).click();
  await assessments.getByRole('button', { name: 'Start Security assessment' }).click();
  await expect(assessments.getByText('Local run state: complete', { exact: true })).toBeVisible();
  expect(fixture.writes.filter(write => write.path.endsWith('/runs'))).toHaveLength(1);
  await assessments.getByRole('link', { name: 'Inspect visible results for this assessment' }).click();
  await expect(page.getByRole('region', { name: 'Security results' })).toBeVisible();
  await page.getByRole('link', { name: 'Runs & schedules', exact: true }).click();
  await page.getByText('Manual trigger and daily schedules', { exact: false }).first().click();
  await page.getByLabel('Daily UTC time for code_review').fill('03:15');
  await page.getByRole('link', { name: 'Setup', exact: true }).click();
  await page.getByText('Edit Security setup', { exact: false }).first().click();
  await page.getByRole('button', { name: 'Save Security configuration' }).click();
  await expect.poll(() => fixture.security().settings.triggers.find(trigger => trigger.trigger_id === 'nightly-code')?.schedule).toBe('15 3 * * *');
  await page.reload(); await page.getByRole('link', { name: 'Runs & schedules', exact: true }).click();
  await page.getByText('Manual trigger and daily schedules', { exact: false }).first().click();
  await expect(page.getByLabel('Daily UTC time for code_review')).toHaveValue('03:15');
  expect(fixture.writes.filter(write => write.path.endsWith('/runs'))).toHaveLength(1);
  expect(fixture.errors).toEqual([]); expect(fixture.external).toEqual([]);
});

test('Insights drill-down, published evidence failure and connection handoff remain scoped', async ({ page }) => {
  const fixture = await install(page); await page.goto(`${root}/insights`);
  await page.getByRole('button', { name: 'View report' }).nth(2).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Definition & evidence' }).nth(4).click();
  await expect(dialog.getByText('Freshness & coverage')).toBeVisible();
  await dialog.getByRole('button', { name: '← Back to report' }).click();
  await dialog.getByRole('button', { name: 'Inspect published business-context evidence' }).click();
  await expect(dialog.getByText('dataset-fixture', { exact: true })).toBeVisible();
  fixture.denyEvidence(); await dialog.getByRole('button', { name: 'Check latest publication' }).click();
  await expect(dialog.getByRole('alert')).toContainText('reconcile and republish');
  await expect(dialog.getByText('dataset-fixture', { exact: true })).toHaveCount(0);
  await page.keyboard.press('Escape'); await page.getByRole('button', { name: 'Data sources & coverage' }).click();
  await expect(dialog.getByText(/Analytics collection not reported/)).toBeVisible();
  await dialog.getByRole('link', { name: 'Review product connections' }).click();
  await expect(page).toHaveURL(/\/settings\?section=basic$/); await expect(dialog).toHaveCount(0);
  expect(fixture.errors).toEqual([]); expect(fixture.external).toEqual([]); expect(fixture.writes).toEqual([]);
});

test('Code scanning opens with the requested capability and submits the full pinned repository', async ({ page }) => {
  const fixture = await install(page); fixture.enableCodeScan(); await page.goto(`${root}/release`);
  await page.getByRole('link', { name: 'Open code scanning' }).click();
  const assessments = page.getByRole('region', { name: 'Security assessments' });
  await expect(assessments.getByLabel('Assessment capability')).toHaveValue('code_review');
  await assessments.getByLabel('Assessment binding').selectOption('binding-one');
  await assessments.getByLabel('full-repository (revision 17)').check();
  await assessments.getByRole('button', { name: 'Check Security readiness' }).click();
  await assessments.getByRole('button', { name: 'Start Security assessment' }).click();
  await expect(assessments.getByText('Local run state: complete', { exact: true })).toBeVisible();
  const request = fixture.writes.find(write => write.path.endsWith('/runs'));
  expect(request?.body).toMatchObject({ capability: 'code_review', sources: [repository] });
  expect(fixture.errors).toEqual([]); expect(fixture.external).toEqual([]);
});

test('DevOps preserves setup, checks run consent, inspects results and gates native UI access', async ({ page }, info) => {
  const fixture = await install(page); await page.goto(`${root}/operate/devops`);
  await expect(page.getByRole('region', { name: 'From Product profile' }).getByText('Operate checkout')).toBeVisible();
  await page.locator('summary').filter({ hasText: 'Provider connection' }).click();
  await page.getByLabel('Existing DevOps bindings').selectOption('devops-binding');
  await page.getByRole('link', { name: 'Runs & schedules', exact: true }).click();
  await page.getByRole('button', { name: 'Check Investigation readiness' }).click();
  await expect(page.getByText(/Product owner must review and grant execution consent/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Submit DevOps run' })).toBeDisabled();
  await page.getByLabel('Investigation question').fill('Why did checkout latency increase?');
  await page.getByRole('button', { name: 'Use saved Investigation resources' }).click();
  fixture.allowDevops(); await page.getByRole('button', { name: 'Check Investigation readiness' }).click();
  await page.getByRole('button', { name: 'Submit DevOps run' }).click();
  await expect(page.getByRole('listitem').filter({ hasText: 'Run investigation-one: complete' })).toBeVisible();
  await page.getByRole('button', { name: 'Check native UI access' }).click();
  await expect(page.getByText(/Review the current native UI grant in Setup/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open native DevOps UI' })).toBeDisabled();
  await expect(page.getByText(/Scheduled investigations: Not available yet/)).toBeVisible();
  await page.getByRole('link', { name: 'Results', exact: true }).click();
  await page.getByRole('button', { name: 'View result', exact: true }).click();
  await expect(page.getByText('Inspect dependency retries: candidate', { exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByLabel('Investigation question')).toHaveValue('Why did checkout latency increase?');
  await page.goForward();
  await expect(page.getByText('Inspect dependency retries: candidate', { exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: info.outputPath('devops-results.png'), fullPage: true });
  expect(fixture.writes.filter(write => write.path.endsWith('/runs'))).toHaveLength(1);
  expect(fixture.writes.filter(write => write.path.endsWith('/browser-launch'))).toHaveLength(0);
  expect(fixture.errors).toEqual([]); expect(fixture.external).toEqual([]);
});


for (const width of [1440, 390]) test(`project budgets and full activity work at ${width}px`, async ({ page }, info) => {
  const fixture = await install(page); fixture.enableBuild();
  await page.setViewportSize({ width, height: 1000 });
  await page.goto(`${root}/projects`);
  const budget = page.getByRole('region', { name: 'Project budget and spend' });
  await expect(budget.getByText('$450.00', { exact: true })).toBeVisible();
  await budget.getByRole('spinbutton').fill('650');
  await budget.getByRole('button', { name: 'Save budget' }).click();
  await expect(budget.getByText('$600.00', { exact: true })).toBeVisible();
  await page.reload();
  await expect(budget.getByRole('spinbutton')).toHaveValue('650');
  await budget.getByRole('link', { name: 'View project delivery flows' }).click();
  await expect(page.getByRole('link', { name: 'New flow' })).toHaveAttribute('href', '/flows/new?product=product-one&project=project-one');
  await page.getByRole('tab', { name: 'Agent activity' }).click();
  await expect(page.getByRole('heading', { name: 'Agent Activity', exact: true })).toBeVisible();
  await page.getByText('Deploy checkout safely', { exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Run workspace' });
  await expect(dialog.getByRole('heading', { name: 'Task checklist' })).toBeVisible();
  await expect(dialog.getByText('Check configuration', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Transcript & run record' }).click();
  await expect(dialog.getByText('Deployment checks completed for the selected project.')).toBeVisible();
  await page.screenshot({ path: info.outputPath(`project-activity-${width}.png`), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(fixture.errors).toEqual([]); expect(fixture.external).toEqual([]);
  expect(fixture.writes).toEqual([{ path: '/api/v1/projects/project-one/budget', method: 'PUT', body: { budget_usd: '650' } }]);
});
