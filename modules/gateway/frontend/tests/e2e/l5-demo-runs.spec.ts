import { test, expect, type Page } from '@playwright/test';
import { configuration as baseConfiguration, binding as baseBinding, selection, resources, policy, consent, grant as baseGrant, reference } from '../../src/__tests__/fixtures/devOpsActivation';
import type { DevOpsConfiguration, DevOpsBinding, SecurityConfiguration, SecurityBindingOperation } from '../../src/services/l5Services';
import type { OwnerGrant } from '../../src/services/l5OwnerConsent';
const now = new Date('2026-11-05T20:00:00Z');
const clone = <T,>(value: T): T => structuredClone(value);
// Fixture credentials are fictional. No request may leave localhost.
test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  await page.clock.install({ time: now });
});
async function install(page: Page, service: 'security' | 'devops', missingInputs = false) {
  const path = `/api/v1/products/product/projects/project/services/${service}`;
  const config = clone(baseConfiguration);
  config.settings.enabled = true; config.state = 'ready';
  config.settings.devops.accounts = [{ account_id: selection.account_id, environments: [{ environment_ref: config.settings.source_refs[0], resource_refs: resources.resource_refs }] }];
  config.settings.devops.trigger_policies = [{ trigger_id: 'saved-manual', kind: 'manual', enabled: true, capability: 'investigation', binding_id: 'binding', environment_ref: config.settings.source_refs[0], resource_refs: resources.resource_refs, telemetry_refs: resources.telemetry_refs }];
  const document = reference('document', 'architecture');
  const repository = { ...reference('repository_commit', '12345'), source_connection_id: 'github' };
  let current: DevOpsConfiguration | SecurityConfiguration = service === 'devops' ? config : { ...config, settings: {
    purpose: 'Review architecture and source code', classification: 'internal', profile_ref: config.settings.profile_ref, connection_ref: reference(), region: 'us-east-1', enabled: true,
    capabilities: ['threat_model', 'code_review'], source_refs: [document, repository], visibility: 'only_me', exclusions: [], overrides: [],
    triggers: [{ trigger_id: 'manual-model', kind: 'manual', enabled: true, capability: 'threat_model', binding_id: 'binding', source_refs: [document], debounce_seconds: 0 },
      { trigger_id: 'manual-code', kind: 'manual', enabled: true, capability: 'code_review', binding_id: 'binding', source_refs: [repository], debounce_seconds: 0 }],
  } } as SecurityConfiguration;
  if (missingInputs) { current.settings.source_refs = []; current.settings.connection_ref = undefined; current.settings.triggers = []; }
  const binding: DevOpsBinding = { ...clone(baseBinding), ownership: 'adp_created', readiness: service === 'devops'
    ? [{ kind: 'resource_access', observation: { resource_selection: selection, expires_at: '2026-11-04T20:00:00Z' } }]
    : [{ kind: 'code_review_source_registration', registration: { bucket: 'example-security-source', service_role: 'arn:aws:iam::123456789012:role/assessment' }, observed_at: '2026-11-04T20:00:00Z' },
      { kind: 'threat_model', native_model_ref: reference('threat_model', 'existing-model'), observed_at: '2026-11-04T20:00:00Z' }] };
  const grants: OwnerGrant[] = [];
  let verified = false;
  const attempts: { path: string; method: string; body: Record<string, unknown>; key?: string }[] = [];
  const runs: Record<string, unknown>[] = [];
  let operation: SecurityBindingOperation | null = null;
  await page.addInitScript(() => {
    const t = Math.floor(Date.now() / 1000);
    const token = `${btoa('{"alg":"RS256"}')}.${btoa(JSON.stringify({ sub: 'owner', email: 'owner@example.test', name: 'Example owner', 'custom:role': 'member', 'custom:org_id': 'tenant', auth_time: t, exp: t + 86400 }))}.${btoa('fixture-signature')}`;
    sessionStorage.setItem('cognito_access_token', token); sessionStorage.setItem('cognito_id_token', token); sessionStorage.setItem('cognito_token_expiry', String((t + 86400) * 1000));
  });
  const preview = (control: boolean) => {
    const scope = { ...clone(consent.scope), service_id: service === 'security' ? 'l5-worker' : 'l5-devops-worker', configuration_revision: current.revision, configuration_hash: current.hash,
      binding_generation: binding.generation + Number(control) };
    if (service === 'security' || !control) {
      delete scope.purpose; delete scope.control_action; delete scope.resource_selection;
      scope.capabilities = service === 'security' ? ['threat_model', 'code_review'] : ['investigation'];
      scope.operations = ['credential:session', 'product:read']; scope.sources = current.settings.source_refs;
    }
    return { scope, scope_digest: 'a'.repeat(64), maximum_lifetime_seconds: 86400 };
  };
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); const target = url.pathname; const method = route.request().method();
    const reply = (value: unknown, etag?: string, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value), headers: etag ? { ETag: etag } : {} });
    if (target === '/api/features') return reply({ features: { product_profile: true } });
    if (target === '/api/access/status') return reply({ status: 'registered' });
    if (target === '/api/auth/workspaces') return reply({ items: [{ org_id: 'tenant', name: 'Example team' }] });
    if (target === '/api/v1/products') return reply({ items: [{ id: 'product', name: 'Demo application', revision: 1, project_ids: ['project'] }] });
    if (target === '/api/v1/products/product/projects/project') return reply({ tenant_id: 'tenant', product_id: 'product', project_id: 'project', revision: 1,
      product: { name: 'Demo application', purpose: 'Review and operate the application', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'owner' } }, project: { id: 'project', name: 'Development', owner_user_id: 'owner' }, configuration: {} });
    if (target.endsWith('/people')) return reply({ product_id: 'product', revision: 1, owner: { id: 'owner', name: 'Example owner' }, team: null, members: [], is_owner: true, permissions: { manage_team: false } });
    if (target === '/api/v1/products/product/projects/project/sources') {
      const aws = { connection_ref: { kind: 'credential', id: 'connection' }, provider: 'aws', label: 'Demo account', coverage: 'partial', resources: [
        { kind: 'environment', native_id: 'us-east-1', source_revision: 'revision', verified_environment: { account_id: '123456789012' } }] };
      const github = { connection_ref: { kind: 'github_installation', id: 'github' }, provider: 'github', coverage: 'complete', resources: [
        { kind: 'repository', native_id: '12345', full_name: 'example/application' }] };
      const id = url.searchParams.get('connection_id');
      return reply({ items: id ? [id === 'connection' ? aws : github] : [aws, github], next_cursor: null });
    }
    if (!target.startsWith(path)) return reply({ detail: { code: 'not_found' } }, undefined, 404);
    if (method === 'GET') {
      if (target.endsWith('/configuration/options')) return reply({ connections: missingInputs ? [] : [reference()], sources: current.settings.source_refs, regions: ['us-east-1'], capabilities: current.settings.capabilities, unavailable_reasons: [] });
      if (target.endsWith('/configuration')) return reply(current, `"${current.hash}"`);
      if (target.endsWith('/bindings')) return reply({ items: [binding] });
      if (target.endsWith('/bindings/binding')) return reply(binding, `"${binding.revision}"`);
      if (target.endsWith('/delegations')) return reply({ items: grants });
      if (target.endsWith('/delegation-scope')) return reply(preview(url.searchParams.get('for_control') === 'true'), `"${current.hash}"`);
      if (target.endsWith('/binding-operations/verification')) return reply(operation);
      if (target.endsWith('/resources')) return reply(resources);
      if (target.endsWith('/access-policy')) return reply({ ...policy, document: { ...policy.document, binding_generation: binding.generation, binding_revision: binding.revision, configuration_revision: current.revision,
        resource_observation: { resource_selection: selection, checked_at: now.toISOString(), expires_at: new Date(now.getTime() + 900_000).toISOString() } } });
      if (target.endsWith('/runs')) return reply({ items: runs });
      if (target.endsWith('/runs/demo-run')) return reply(runs[0], '"1"');
      return reply({ items: [] });
    }
    const body = route.request().postData() ? route.request().postDataJSON() : {};
    if (target.endsWith('/delegation-scope')) return reply(preview(true), `"${current.hash}"`);
    if (target.endsWith('/configuration/check')) return reply(service === 'security' ? { ready: verified, gaps: verified ? [] : ['execution_delegation', 'native_unqualified'], provider_contacted: false } : {
      ready: verified && grants.some(grant => !grant.revoked_at && !grant.execution_scope?.purpose), gaps: verified ? [] : ['delegation_denied'], scope: current.scope, capability: 'investigation', configuration_id: current.configuration_id,
      configuration_revision: current.revision, configuration_hash: current.hash, binding_id: 'binding', binding_generation: binding.generation, binding_revision: binding.revision,
      checked_at: now.toISOString(), valid_until: new Date(now.getTime() + 60_000).toISOString(),
    });
    attempts.push({ path: target, method, body, key: route.request().headers()['idempotency-key'] });
    if (target.endsWith('/configuration') && method === 'PUT') { current = { ...current, settings: { ...current.settings, ...body }, revision: current.revision + 1, hash: `hash-${current.revision + 1}` }; return reply(current, `"${current.hash}"`); }
    if (target.endsWith('/delegations')) {
      const scope = preview(service === 'security' ? body.for_control : Boolean(body.control_action)).scope;
      const grant = { ...clone(baseGrant), service_id: scope.service_id, execution_scope: scope, expires_at: body.expires_at, delegation_id: `grant-${grants.length + 1}`, capabilities: scope.capabilities };
      grants.push(grant); return reply(grant, '"1"');
    }
    if (method === 'DELETE') { const grant = grants.find(item => target.endsWith('/' + item.delegation_id))!; grant.revoked_at = now.toISOString(); grant.revision++; return reply(grant, `"${grant.revision}"`); }
    if (target.endsWith('/binding-operations')) {
      binding.generation++; binding.revision++; binding.readiness.forEach(item => { item.observed_at = now.toISOString(); });
      verified = true; operation = { operation_id: 'verification', binding_id: 'binding', state: 'succeeded', revision: 1, action: 'check', gaps: [] }; return reply(operation, '"1"');
    }
    if (target.endsWith('/runs')) {
      runs.push({ run_id: 'demo-run', scope: current.scope, revision: 1, state: 'running', provider_state: 'running', origin: 'adp_submitted', input: body,
        deadline_at: new Date(now.getTime() + 3600_000).toISOString() }); return reply(runs[0], '"1"', 202);
    }
    return reply({ detail: { code: 'not_found' } }, undefined, 404);
  });
  return { attempts, runs, current: () => current };
}
for (const service of ['security', 'devops'] as const) for (const mobile of [false, true]) {
  test(`${service}: expired access to start, ${mobile ? 'mobile' : 'desktop'}`, async ({ page }, info) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    const fixture = await install(page, service);
    await page.goto(`/products/product/projects/project/services/${service}`);
    await page.getByRole('link', { name: 'Runs & schedules', exact: true }).click();
    if (service === 'devops') {
      await page.getByLabel('Investigation question').fill('Why did the error rate rise in the last hour?');
      await page.getByRole('button', { name: 'Start investigation', exact: true }).click();
      await expect(page.getByText(/Not ready to start/)).toBeVisible();
      expect(fixture.runs).toHaveLength(0);
    }
    else await expect(page.getByRole('region', { name: 'Security assessments' }).getByLabel('architecture (revision revision)')).toBeChecked();
    await page.getByRole('button', { name: 'Refresh run access', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Refresh run access' });
    await expect(dialog.getByText('Saved target', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Review access', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: 'Allow verification' })).toBeVisible();
    await page.screenshot({ path: info.outputPath('access-review.png'), fullPage: true });
    await dialog.getByRole('checkbox').check();
    await dialog.getByRole('button', { name: 'Allow access and verify' }).click();
    if (service === 'devops') {
      await expect(dialog.getByRole('heading', { name: 'Allow runs' })).toBeVisible();
      await dialog.getByRole('checkbox').check(); await dialog.getByRole('button', { name: 'Allow runs', exact: true }).click();
    }
    await expect(dialog).toHaveCount(0);
    expect(fixture.attempts.filter(item => item.path.endsWith('/runs'))).toHaveLength(0);
    if (service === 'security' && !mobile) {
      await page.getByLabel('Assessment capability').selectOption('code_review');
      await expect(page.getByRole('region', { name: 'Security assessments' }).getByLabel('example/application (revision revision)')).toBeChecked();
    }
    const start = page.getByRole('button', { name: service === 'security' ? 'Start Security assessment' : 'Start investigation', exact: true });
    await expect(start).toBeEnabled();
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await page.screenshot({ path: info.outputPath('ready-to-run.png'), fullPage: true });
    await start.click(); await expect.poll(() => fixture.runs.length).toBe(1);
    await expect(page.getByText('running', { exact: true }).first()).toBeVisible();
    expect(fixture.attempts.filter(item => item.path.endsWith('/binding-operations'))).toHaveLength(1);
    expect(fixture.attempts.filter(item => item.path.endsWith('/runs'))).toHaveLength(1);
    expect(fixture.current().settings.enabled).toBe(true);
    expect(errors).toEqual([]);
    expect(await page.locator('body').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  });
}

for (const mobile of [false, true]) {
  test(`Security setup: visible choices and focused tools, ${mobile ? 'mobile' : 'desktop'}`, async ({ page }, info) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    const fixture = await install(page, 'security');
    await page.goto('/products/product/projects/project/services/security#l5-security-capabilities');
    await expect(page.getByRole('heading', { name: 'What do you want to check?' })).toBeVisible();
    await expect(page.getByRole('checkbox', { name: 'Threat modeling', exact: true })).toBeChecked();
    await expect(page.getByRole('checkbox', { name: 'Code scanning', exact: true })).toBeChecked();
    await expect(page.getByRole('link', { name: 'Capabilities', exact: true })).toHaveCount(0);
    await expect(page.getByText('Pen testing · Not available yet')).toBeVisible();
    await expect(page.getByRole('option', { name: '123456789012 · Demo account' })).toHaveCount(1);
    await expect(page.getByRole('group', { name: 'Source revisions for Security' }).getByText('example/application', { exact: true })).toBeVisible();
    await expect(page.getByLabel('AWS account')).toHaveValue('0');
    await expect(page.getByLabel('AWS region', { exact: false }).first()).toHaveValue('us-east-1');
    await expect(page.getByRole('group', { name: 'Source revisions for Security' }).getByRole('checkbox', { name: /architecture/ })).toBeChecked();
    await expect(page.getByText('Review architecture and source code', { exact: true })).toBeVisible();
    await expect(page.getByLabel('Maximum cost per assessment (USD)')).not.toBeVisible();
    await expect(page.getByLabel('Existing Security resource ID')).not.toBeVisible();
    await page.screenshot({ path: info.outputPath('security-setup.png'), fullPage: true });
    const workspaceButton = page.getByRole('button', { name: 'Manage workspace', exact: true });
    await workspaceButton.click();
    const workspace = page.getByRole('dialog', { name: 'Security workspace', exact: true });
    await expect(workspace.getByLabel('Saved workspace')).toHaveValue('binding');
    await expect(workspace.getByRole('button', { name: 'Verify workspace' })).toBeEnabled();
    await expect(workspace.getByText('Registered execution role')).not.toBeVisible();
    await page.screenshot({ path: info.outputPath('workspace.png'), fullPage: true });
    await workspace.getByRole('button', { name: 'Close Security workspace', exact: true }).click();
    await expect(workspaceButton).toBeFocused();
    await page.getByRole('button', { name: 'Advanced settings', exact: true }).click();
    const advanced = page.getByRole('dialog', { name: 'Advanced Security settings' });
    await advanced.getByText('Advanced release verification', { exact: true }).click();
    await advanced.getByLabel('Request ID', { exact: true }).fill('example-request');
    await advanced.getByRole('button', { name: 'Close Advanced Security settings' }).click();
    await expect(workspaceButton).toBeEnabled();
    expect(fixture.attempts).toHaveLength(0);
    await page.getByRole('button', { name: 'Review profile updates', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Read current Product profile' })).toBeVisible();
    await page.getByRole('button', { name: 'Close Product profile updates' }).click();
    await page.getByRole('button', { name: 'Refresh run access', exact: true }).click();
    const access = page.getByRole('dialog', { name: 'Refresh run access' });
    await access.getByRole('button', { name: 'Review access', exact: true }).click();
    await access.getByLabel('Access duration').selectOption('1');
    await access.getByRole('checkbox').check();
    await access.getByRole('button', { name: 'Close Refresh run access' }).click();
    await expect(workspaceButton).toBeEnabled();
    expect(fixture.attempts).toHaveLength(0);
    await page.getByLabel('Who can see results?').selectOption('product_team');
    await expect(page.getByText(/You have unsaved changes/)).toBeVisible();
    await expect(workspaceButton).toBeDisabled();
    await page.getByRole('button', { name: 'Save setup', exact: true }).click();
    await expect(page.getByText('Security setup saved. Open Runs & schedules to start an assessment.')).toBeVisible();
    const save = fixture.attempts.find(item => item.method === 'PUT' && item.path.endsWith('/configuration'))!;
    expect(save.body.visibility).toBe('product_team');
    expect(save.body).not.toHaveProperty('limits');
    expect((save.body.triggers as unknown[]).length).toBe(2);
    expect(errors).toEqual([]);
    expect(await page.locator('body').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.getByRole('link', { name: 'Go to Runs & schedules →' }).click();
    await expect(page.getByRole('button', { name: 'Start Security assessment', exact: true })).toBeVisible();
  });
}
test('Security setup explains missing Product inputs and links to its profile', async ({ page }) => {
  await install(page, 'security', true);
  await page.goto('/products/product/projects/project/services/security');
  await expect(page.getByText('No architecture document is available. Add and verify one in the Product profile.')).toBeVisible();
  await expect(page.getByText('No repository is available. Connect and verify a repository in the Product profile.')).toBeVisible();
  await expect(page.getByText('No AWS account is available. Connect an AWS account in the Product profile before connecting the Security workspace.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open Product profile', exact: true })).toHaveAttribute('href', '/products/product/projects/project');
});
