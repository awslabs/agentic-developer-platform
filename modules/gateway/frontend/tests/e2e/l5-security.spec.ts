import { expect, test, type Page, type Route } from '@playwright/test';

type Reference = { tenant_id: string; project_id: string; source_connection_id: string; record_type: string; record_id: string; record_revision: string };
type SavedConfiguration = { scope: { tenant_id: string; product_id: string; project_id: string; relationship_revision: string; owner_id: string; visibility: string }; configuration_id: string; revision: number; state: string; handoff_state: string; hash: string;
  settings: Record<string, unknown>; inherited_fields: string[]; gaps: string[] };

const baseUrl = 'http://127.0.0.1:5187';
const profile = (tenantId: string, productId: string) => ({
  tenant_id: tenantId, product_id: productId, project_id: `project-${tenantId}`, revision: 5,
  product: { name: `Planner ${tenantId}`, purpose: `Plan ${tenantId} releases`, intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: `actor-${tenantId}` } },
  project: { id: `project-${tenantId}`, name: 'Delivery scope', owner_user_id: `actor-${tenantId}` },
  configuration: {},
});

async function installSecurityFixture(page: Page, tenantId: string) {
  const productId = `product-${tenantId}`;
  const projectId = `project-${tenantId}`;
  const scope = `/api/v1/products/${productId}/projects/${projectId}`;
  const security = `${scope}/services/security`;
  const source: Reference = { tenant_id: tenantId, project_id: projectId, source_connection_id: 'connection-one', record_type: 'document', record_id: 'strategy', record_revision: '17' };
  const connections: Reference[] = ['connection-one', 'connection-two'].map((id) => ({ tenant_id: tenantId, project_id: projectId,
    source_connection_id: id, record_type: 'connection', record_id: id, record_revision: '7' }));
  let saved: SavedConfiguration | null = null;
  let faultNextOptions = false;
  const requests: { method: string; path: string }[] = [];
  const attempts: { etag: string | undefined; key: string | undefined; body: Record<string, unknown> }[] = [];

  await page.addInitScript((actorTenant) => {
    const now = Math.floor(Date.now() / 1000);
    const token = `${btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))}.${btoa(JSON.stringify({
      sub: `actor-${actorTenant}`, email: `owner-${actorTenant}@example.test`, name: 'Example owner', auth_time: now,
      'custom:role': 'member', 'custom:org_id': actorTenant, exp: now + 3600,
    }))}.${btoa('test-signature')}`;
    sessionStorage.setItem('cognito_access_token', token);
    sessionStorage.setItem('cognito_id_token', token);
    sessionStorage.setItem('cognito_token_expiry', String((now + 3600) * 1000));
  }, tenantId);

  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    requests.push({ method, path });
    const respond = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers });
    if (path === '/api/features') return respond({ features: { product_profile: true } });
    if (path === '/api/access/status') return respond({ status: 'registered' });
    if (path === '/api/auth/workspaces') return respond({ items: [{ org_id: tenantId, name: 'Example organization' }] });
    if (path === '/api/v1/products') return respond({ items: [{ id: productId, name: `Planner ${tenantId}`, revision: 5, project_ids: [projectId] }], next_cursor: null });
    if (path === scope) return respond(profile(tenantId, productId));
    if (path === `${scope}/people`) return respond({ product_id: productId, revision: 5,
      owner: { id: `actor-${tenantId}`, name: 'Example owner', github_username: null }, team: null, members: [], next_offset: null, is_owner: true,
      permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    if (path === `${security}/configuration/options`) {
      if (faultNextOptions) { faultNextOptions = false; return route.abort('failed'); }
      return respond({ connections, sources: [source], regions: [], capabilities: ['threat_model'], unavailable_reasons: ['region_options_unavailable'] });
    }
    if (path === `${security}/configuration` && method === 'GET')
      return saved ? respond(saved, 200, { ETag: `"${saved.hash}"` }) : respond({ detail: { code: 'not_found' } }, 404);
    if (path === `${security}/configuration` && method === 'PUT') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      const headers = route.request().headers();
      attempts.push({ etag: headers['if-match'] ?? headers['if-none-match'], key: headers['idempotency-key'], body });
      if (!headers['idempotency-key'] || (saved ? headers['if-match'] !== `"${saved.hash}"` : headers['if-none-match'] !== '*'))
        return respond({ detail: { code: 'stale_revision' } }, 412);
      saved = { scope: { tenant_id: tenantId, product_id: productId, project_id: projectId, relationship_revision: '5', owner_id: `actor-${tenantId}`, visibility: 'only_me' }, configuration_id: `config-${tenantId}`, revision: (saved?.revision ?? 0) + 1, state: 'needs_input', handoff_state: 'linked',
        hash: `config-${tenantId}-${attempts.length}`, inherited_fields: ['purpose', 'source_refs'], gaps: ['execution_delegation'],
        settings: { profile_ref: { ...source, record_type: 'product', record_id: productId, record_revision: '5' },
          purpose: `Plan ${tenantId} releases`, source_refs: [source], ...body } };
      return respond(saved, saved.revision === 1 ? 201 : 200, { ETag: `"${saved.hash}"` });
    }
    if (path === `${security}/bindings` || (method === 'GET' && (path === `${security}/runs` || path === `${security}/results`))) return respond({ items: [] });
    if (path.startsWith('/api/v1/products/') && !path.startsWith(scope)) return respond({ detail: { code: 'not_found' } }, 404);
    return respond({ detail: { code: 'not_found' } }, 404);
  });

  return { productId, projectId, scope, security, source, connections, requests, attempts,
    saved: () => saved, faultNextOptions: () => { faultNextOptions = true; } };
}

test('configure_security: Product workspace setup is scoped, versioned and side-effect free', async ({ page, browser }) => {
  await page.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
  const first = await installSecurityFixture(page, 'tenant-a');
  await page.goto(`/products/${first.productId}/projects/${first.projectId}`);
  await page.getByRole('navigation', { name: 'Product workspace' }).getByRole('button', { name: 'Context & services' }).click();
  await expect(page).toHaveURL(new RegExp(`/products/${first.productId}/projects/${first.projectId}/services/security$`));
  const setup = page.getByRole('region', { name: 'Security configuration' });
  await expect(setup.getByText('No Security configuration is saved yet. Select a capability to begin.')).toBeVisible();
  first.faultNextOptions();
  await setup.getByRole('button', { name: 'Refresh Security configuration' }).click();
  await expect(setup.getByRole('alert')).toContainText('Security could not be loaded');
  await setup.getByRole('button', { name: 'Refresh Security configuration' }).click();
  await expect(setup.getByRole('combobox', { name: 'AWS account' })).toBeVisible();
  await setup.getByLabel('threat model').check();
  await setup.getByRole('combobox', { name: 'AWS account' }).selectOption('1');
  await setup.getByLabel('AWS region').fill('us-east-1');
  await setup.getByLabel('Reason for explicit Security choices').fill('Verified Security account and region');
  await setup.getByRole('button', { name: 'Save Security configuration' }).click();
  await expect(setup.getByText(/Security configuration saved at revision 1/)).toBeVisible();
  expect(first.attempts).toHaveLength(1);
  expect(first.attempts[0]).toMatchObject({ etag: '*', key: expect.any(String), body: { capabilities: ['threat_model'], region: 'us-east-1', connection_ref: first.connections[1],
    overrides: expect.arrayContaining([expect.objectContaining({ field: 'region', reason: 'Verified Security account and region' })]) } });
  expect(first.attempts[0].body).not.toHaveProperty('source_refs');
  expect(first.saved()?.settings.source_refs).toEqual([first.source]);
  expect(first.requests.filter(request => request.method !== 'GET' && /binding-operations|\/runs/.test(request.path))).toHaveLength(0);
  const readback = await page.evaluate(async (path) => {
    const response = await fetch(path); return { etag: response.headers.get('ETag'), body: await response.json() };
  }, `${first.security}/configuration`);
  expect(readback.etag).toBe('"config-tenant-a-1"');
  expect(readback.body).toMatchObject({ revision: 1, inherited_fields: ['purpose', 'source_refs'], settings: { source_refs: [first.source], connection_ref: first.connections[1] } });
  await page.reload();
  await expect(page.getByRole('region', { name: 'Security configuration' }).getByText('1', { exact: true })).toBeVisible();
  expect(first.attempts).toHaveLength(1);

  await page.goto('/products/product-tenant-b/projects/project-tenant-b/services/security');
  await expect(page.getByText('Product unavailable or you do not have access.')).toBeVisible();
  expect(first.saved()?.revision).toBe(1);

  const other = await browser.newContext({ baseURL: baseUrl });
  try {
    const otherPage = await other.newPage();
    await otherPage.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
    const second = await installSecurityFixture(otherPage, 'tenant-b');
    await otherPage.goto(`/products/${second.productId}/projects/${second.projectId}/services/security`);
    await expect(otherPage.getByRole('region', { name: 'Security configuration' }).getByText('No Security configuration is saved yet. Select a capability to begin.')).toBeVisible();
    expect(second.saved()).toBeNull();
    expect(first.saved()?.revision).toBe(1);
  } finally { await other.close(); }
});

async function installAssessmentFixture(page: Page, tenantId: string) {
  const setup = await installSecurityFixture(page, tenantId);
  const { productId, projectId, security, source } = setup;
  const bindingId = `binding-${tenantId}`;
  const binding = { scope: { tenant_id: tenantId, product_id: productId, project_id: projectId, relationship_revision: '5' }, binding_id: bindingId, configuration_id: `config-${tenantId}`, state: 'ready', revision: 2, generation: 3,
    ownership: 'adp_created', connection_ref: setup.connections[0], gaps: [], readiness: [{ capability: 'threat_model', state: 'ready', reason_codes: [] }] };
  const configuration = { scope: binding.scope, configuration_id: binding.configuration_id, revision: 4, state: 'ready', handoff_state: 'linked',
    hash: `config-${tenantId}-4`, inherited_fields: ['purpose', 'source_refs'], gaps: [], settings: { capabilities: ['threat_model'], enabled: true,
      visibility: 'only_me', connection_ref: setup.connections[0], region: 'us-east-1', source_refs: [source], exclusions: [], overrides: [],
      monitoring: { poll_interval_seconds: 120, poll_grace_seconds: 180 }, triggers: [{ trigger_id: 'manual-one', kind: 'manual', enabled: true,
        capability: 'threat_model', binding_id: bindingId, source_refs: [source], debounce_seconds: 0 }] } };
  const scope = { tenant_id: tenantId, product_id: productId, project_id: projectId, relationship_revision: '5',
    owner_id: `actor-${tenantId}`, visibility: 'only_me' };
  const job = { ...source, record_type: 'native_job', record_id: 'job-one' };
  const run = { run_id: 'run-one', scope, revision: 2, state: 'partial', provider_state: 'failed', deadline_at: '2026-10-27T13:00:00Z',
    monitoring: { state: 'degraded', progress_state: 'suspected_stalled', poll_attempts: 3, consecutive_poll_errors: 1,
      last_poll_attempt_at: '2026-10-27T12:05:00Z', last_successful_poll_at: '2026-10-27T12:03:00Z', next_poll_at: '2026-10-27T12:07:00Z',
      latest_observation: { native_status: 'FAILED', outcome: 'failed', progress_coverage: 'partial', phases: [{ name: 'preflight', native_status: 'COMPLETED' }],
        tasks: { running: 0, completed: 2, failed: 1, stopped: 0, unknown: 0 } } },
    admission: { monitoring_policy: { poll_interval_seconds: 120, poll_grace_seconds: 180, stall_after_seconds: 7200 } } };
  const result = { result_id: 'result-one', run_id: 'run-one', scope, revision: 2, native_job_ref: job, observed_at: '2026-10-27T12:05:00Z',
    coverage: 'partial', coverage_mode: 'documents', provider_outcome: 'failed', finding_count: 1, publication_id: 'publication-one',
    admission: { adapter_revision: 'adapter-1', binding_generation: 3, source_snapshots: [{ snapshot_id: 'snapshot-one', source_ref: source }] } };
  const finding = { finding_id: 'finding-one', native_id: 'native-one', native_job_ref: job, kind: 'candidate_threat', claim: 'Review access to strategy',
    provider_severity: 'high', validation: 'candidate', source_refs: [source], evidence: [], locations: ['strategy'],
    assumptions: ['Access is current'], mitigations: ['Review policy'] };
  let admitted = false;
  let denyFindings = true;
  let faultNextResults = false;
  const runAttempts: { key: string | undefined; body: Record<string, unknown> }[] = [];
  await page.route(`${baseUrl}${security}/**`, async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const respond = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers });
    if (path === `${security}/configuration` && method === 'GET') return respond(configuration, 200, { ETag: `"${configuration.hash}"` });
    if (path === `${security}/bindings` && method === 'GET') return respond({ items: [binding] });
    if (path === `${security}/runs` && method === 'GET') return respond({ items: admitted ? [run] : [] });
    if (path === `${security}/runs` && method === 'POST') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      const key = route.request().headers()['idempotency-key'];
      runAttempts.push({ key, body });
      if (!key || body.configuration_revision !== 4 || body.binding_id !== bindingId || body.capability !== 'threat_model'
        || JSON.stringify(body.sources) !== JSON.stringify([source])) return respond({ detail: { code: 'invalid_admission' } }, 422);
      admitted = true;
      return respond({ ...run, state: 'admitted', provider_state: 'not_started' }, 202, { ETag: '"run-one"' });
    }
    if (path === `${security}/runs/run-one` && method === 'GET') return admitted ? respond(run, 200, { ETag: '"run-two"' }) : respond({ detail: { code: 'not_found' } }, 404);
    if (path === `${security}/results` && method === 'GET') {
      if (faultNextResults) { faultNextResults = false; return route.abort('failed'); }
      return respond({ items: admitted ? [result] : [] });
    }
    if (path === `${security}/results/result-one` && method === 'GET') return admitted ? respond(result) : respond({ detail: { code: 'not_found' } }, 404);
    if (path === `${security}/results/result-one/findings` && method === 'GET') return denyFindings
      ? respond({ detail: { code: 'source_access_denied' } }, 403) : respond({ items: [finding] });
    if (path === `${security}/results/result-one/findings/finding-one/triage` && method === 'GET') return respond({ items: [] }, 200, { ETag: '"empty-triage-head"' });
    if (path === `${security}/publications/publication-one` && method === 'GET') return respond({ publication_id: 'publication-one', result_id: 'result-one',
      state: 'retry_wait', contribution_hash: 'hash-one', invalidation_watermark: 0 });
    return route.fallback();
  });
  return { ...setup, run, result, finding, runAttempts, bindingId,
    faultNextResults: () => { faultNextResults = true; }, restoreSourceAccess: () => { denyFindings = false; } };
}

test('run_and_inspect: partial assessments and denied source findings stay scoped', async ({ page, browser }) => {
  await page.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
  const first = await installAssessmentFixture(page, 'tenant-a');
  await page.goto(`/products/${first.productId}/projects/${first.projectId}/services/security`);
  const assessments = page.getByRole('region', { name: 'Security assessments' });
  const results = page.getByRole('region', { name: 'Security results' });
  await expect(assessments.getByText(/No assessments are visible/)).toBeVisible();
  await expect(results.getByText(/No Security results are visible/)).toBeVisible();
  await assessments.getByLabel('Assessment capability').selectOption('threat_model');
  await assessments.getByLabel('Assessment binding').selectOption(first.bindingId);
  await assessments.getByLabel('strategy (revision 17)').check();
  await assessments.getByRole('button', { name: 'Start Security assessment' }).click();
  await expect.poll(() => first.runAttempts.length).toBe(1);
  expect(first.runAttempts[0]).toMatchObject({ key: expect.any(String), body: { configuration_revision: 4, binding_id: first.bindingId,
    capability: 'threat_model', sources: [first.source] } });
  const runSelect = assessments.getByRole('combobox', { name: 'Visible assessments' });
  await runSelect.focus(); await runSelect.press('ArrowDown'); await runSelect.press('Enter');
  await expect(assessments.getByText('Local run state: partial')).toBeVisible();
  await expect(assessments.getByText('Provider state: failed')).toBeVisible();
  await expect(assessments.getByText(/Monitoring freshness: degraded; poll attempts: 3/)).toBeVisible();
  await expect(assessments.getByText(/Native job status: FAILED. Progress coverage: partial/)).toBeVisible();
  await assessments.getByRole('link', { name: 'Inspect visible results for this assessment' }).click();
  await expect(page).toHaveURL(/#security-results$/);
  first.faultNextResults();
  await results.getByRole('button', { name: 'Refresh Security results' }).click();
  await expect(results.getByRole('alert')).toContainText('No finding count or coverage is confirmed');
  await results.getByRole('button', { name: 'Refresh Security results' }).click();
  const resultSelect = results.getByRole('combobox', { name: 'Visible results' });
  await resultSelect.focus(); await resultSelect.press('ArrowDown'); await resultSelect.press('Enter');
  await expect(page.getByRole('alert')).toContainText('Previously shown details have been removed');
  await expect(results).toHaveCount(0);
  await expect(page.getByText(/Reported finding count/)).toHaveCount(0);
  first.restoreSourceAccess();
  await page.getByRole('button', { name: 'Refresh Security access' }).click();
  await resultSelect.selectOption('result-one');
  await expect(results.getByText(/Reported finding count: 1.*Coverage is incomplete/)).toBeVisible();
  await expect(results.getByText(/Publication state: retry_wait.*not confirmed/)).toBeVisible();
  await results.getByRole('combobox', { name: 'Visible findings' }).selectOption('finding-one');
  await expect(results.getByText(/Candidate threat finding-one.*Review access to strategy/)).toBeVisible();
  await expect(results.getByText(/validation: candidate; human triage: not recorded/)).toBeVisible();
  const readback = await page.evaluate(async ([runPath, resultPath]) => {
    const runResponse = await fetch(runPath);
    const resultResponse = await fetch(resultPath);
    return { run: await runResponse.json(), runEtag: runResponse.headers.get('ETag'), result: await resultResponse.json() };
  }, [`${first.security}/runs/run-one`, `${first.security}/results/result-one`]);
  expect(readback).toMatchObject({ run: { run_id: 'run-one', state: 'partial', provider_state: 'failed' }, runEtag: '"run-two"',
    result: { result_id: 'result-one', coverage: 'partial', finding_count: 1, scope: { tenant_id: 'tenant-a', product_id: first.productId } } });
  expect(first.runAttempts).toHaveLength(1);
  const other = await browser.newContext({ baseURL: baseUrl });
  try {
    const otherPage = await other.newPage();
    await otherPage.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
    const second = await installAssessmentFixture(otherPage, 'tenant-b');
    await otherPage.goto(`/products/${second.productId}/projects/${second.projectId}/services/security`);
    await expect(otherPage.getByRole('region', { name: 'Security results' }).getByText(/No Security results are visible/)).toBeVisible();
    const denied = await otherPage.evaluate(async (path) => (await fetch(path)).status, `${first.security}/results/result-one`);
    expect(denied).toBe(404);
    expect(second.runAttempts).toHaveLength(0);
  } finally { await other.close(); }
});


test('cross_client_and_revocation: stale UI edits never overwrite CLI changes or reveal foreign counts', async ({ page, browser }) => {
  await page.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
  const first = await installSecurityFixture(page, 'tenant-a');
  const ownResult = { scope: { tenant_id: 'tenant-a', product_id: first.productId, project_id: first.projectId, relationship_revision: '5' },
    result_id: 'result-one', run_id: 'run-one', revision: 2, observed_at: '2026-10-27T12:00:00Z',
    coverage: 'partial', coverage_mode: 'documents', finding_count: 93, provider_outcome: 'failed',
    native_job_ref: { ...first.source, record_type: 'native_job', record_id: 'job-one' },
    admission: { adapter_revision: 'adapter-one', binding_generation: 2, source_snapshots: [] } };
  let revoked = false;
  let foreignResult = false;
  const resultReads: string[] = [];
  await page.route(`${baseUrl}${first.security}/results**`, async route => {
    const path = new URL(route.request().url()).pathname;
    resultReads.push(path);
    const respond = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (revoked) return respond({ detail: { code: 'not_found' } }, 404);
    if (path === `${first.security}/results`) return respond({ items: [foreignResult
      ? { ...ownResult, result_id: 'foreign-result', scope: { ...ownResult.scope, tenant_id: 'tenant-b', product_id: 'product-tenant-b' }, finding_count: 927 }
      : ownResult] });
    if (path === `${first.security}/results/result-one`) return respond(ownResult);
    if (path === `${first.security}/results/result-one/findings`) return respond({ items: [] });
    return respond({ detail: { code: 'not_found' } }, 404);
  });

  await page.goto(`/products/${first.productId}/projects/${first.projectId}/services/security`);
  const setup = page.getByRole('region', { name: 'Security configuration' });
  await expect(setup.getByText('No Security configuration is saved yet. Select a capability to begin.')).toBeVisible();
  first.faultNextOptions();
  await setup.getByRole('button', { name: 'Refresh Security configuration' }).click();
  await expect(setup.getByRole('alert')).toContainText('Security could not be loaded');
  await setup.getByRole('button', { name: 'Refresh Security configuration' }).click();
  await expect(setup.getByLabel('threat model')).toBeVisible();
  await setup.getByLabel('threat model').check();
  await setup.getByLabel('AWS account').selectOption('0');
  await setup.getByLabel('AWS region').fill('us-east-1');
  await setup.getByLabel('Reason for explicit Security choices').fill('Verified account and region');
  await setup.getByRole('button', { name: 'Save Security configuration' }).click();
  await expect(setup.getByText(/saved at revision 1/)).toBeVisible();
  const results = page.getByRole('region', { name: 'Security results' });
  await results.getByLabel('Visible results').selectOption('result-one');
  await expect(results.getByText(/Reported finding count: 93.*Coverage is incomplete/)).toBeVisible();

  const cliResponse = await page.evaluate(async path => {
    const response = await fetch(path, { method: 'PUT', headers: { 'Content-Type': 'application/json',
      'Idempotency-Key': 'cli-equivalent-revision-two', 'If-Match': '"config-tenant-a-1"' },
    body: JSON.stringify({ region: 'us-east-2', capabilities: ['threat_model'], enabled: false, visibility: 'only_me',
      exclusions: [], overrides: [{ field: 'region', reason: 'CLI-equivalent selection', value: 'us-east-2' }], triggers: [] }) });
    return { status: response.status, etag: response.headers.get('ETag'), value: await response.json() };
  }, `${first.security}/configuration`);
  expect(cliResponse).toMatchObject({ status: 200, etag: '"config-tenant-a-2"', value: { revision: 2, settings: { region: 'us-east-2' } } });
  await setup.getByLabel('AWS region').fill('us-west-2');
  await setup.getByLabel('Reason for explicit Security choices').fill('Old UI selection');
  await setup.getByRole('button', { name: 'Save Security configuration' }).click();
  await expect(setup.getByRole('alert')).toContainText('changed elsewhere');
  expect(first.saved()?.revision).toBe(2);
  expect(first.saved()?.settings.region).toBe('us-east-2');
  expect(first.attempts).toHaveLength(3);
  expect(first.attempts[2]).toMatchObject({ etag: '"config-tenant-a-1"', body: { region: 'us-west-2' } });
  await expect(setup.getByRole('button', { name: 'Save Security configuration' })).toBeDisabled();
  await setup.getByRole('button', { name: 'Refresh Security configuration' }).click();
  await expect(setup.getByLabel('AWS region')).toHaveValue('us-east-2');
  await expect(setup.getByRole('button', { name: 'Save Security configuration' })).toBeDisabled();
  expect(first.attempts).toHaveLength(3);
  await setup.getByLabel('AWS region').fill('us-west-1');
  await setup.getByLabel('Reason for explicit Security choices').fill('New UI selection after reading revision two');
  await setup.getByRole('button', { name: 'Save Security configuration' }).click();
  await expect(setup.getByText(/saved at revision 3/)).toBeVisible();
  expect(first.attempts).toHaveLength(4);
  expect(first.attempts[3]).toMatchObject({ etag: '"config-tenant-a-2"', body: { region: 'us-west-1' } });
  const readback = await page.evaluate(async path => {
    const response = await fetch(path); return { etag: response.headers.get('ETag'), value: await response.json() };
  }, `${first.security}/configuration`);
  expect(readback).toMatchObject({ etag: '"config-tenant-a-4"', value: { revision: 3, settings: { region: 'us-west-1' } } });
  await results.getByLabel('Visible results').selectOption('result-one');
  await expect(results.getByText(/Reported finding count: 93/)).toBeVisible();

  revoked = true;
  await results.getByRole('button', { name: 'Refresh Security results' }).click();
  await expect(page.getByRole('alert')).toContainText('Previously shown details have been removed');
  await expect(page.getByRole('region', { name: 'Security configuration' })).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Security results' })).toHaveCount(0);
  await expect(page.getByText(/Reported finding count: 93/)).toHaveCount(0);
  const deniedRead = await page.evaluate(async path => (await fetch(path)).status, `${first.security}/results/result-one`);
  expect(deniedRead).toBe(404);
  expect(resultReads).toContain(`${first.security}/results`);
  revoked = false;
  await page.getByRole('button', { name: 'Refresh Security access' }).click();
  await expect(page.getByRole('region', { name: 'Security configuration' }).getByText('3', { exact: true })).toBeVisible();
  foreignResult = true;
  await page.getByRole('region', { name: 'Security results' }).getByRole('button', { name: 'Refresh Security results' }).click();
  await expect(page.getByRole('alert')).toContainText('Previously shown details have been removed');
  await expect(page.getByText(/927|foreign-result|Reported finding count/)).toHaveCount(0);

  const other = await browser.newContext({ baseURL: baseUrl });
  try {
    const otherPage = await other.newPage();
    await otherPage.clock.install({ time: new Date('2026-10-27T12:00:00Z') });
    const second = await installSecurityFixture(otherPage, 'tenant-b');
    await otherPage.goto(`/products/${second.productId}/projects/${second.projectId}/services/security`);
    await expect(otherPage.getByRole('region', { name: 'Security configuration' }).getByText('No Security configuration is saved yet. Select a capability to begin.')).toBeVisible();
    expect(await otherPage.evaluate(async path => (await fetch(path)).status, `${first.security}/results/result-one`)).toBe(404);
    expect(second.saved()).toBeNull();
    expect(first.attempts).toHaveLength(4);
  } finally { await other.close(); }
});
