import { expect, test, type Page } from '@playwright/test';

const flowId = 'fixture-flow-activity';
const node = (id: string, wave: string, state: string) => ({
  id, epic_ref: 'epic-1', wave_ref: wave, node_ref: id, kind: 'story', title: id,
  state, stalled: false, issue_ref: null, attempts: 0, run_id: null,
  cost: { status: 'unknown', reason: 'not_started' },
});
const graph = {
  activity_included: false, flow_id: flowId, slug: flowId, title: 'Activity fixture graph',
  intent_ref: null, state: 'pending', execution_paused: true,
  cost: { status: 'unknown', reason: 'no_usage_rows', partial: true },
  nodes: [node('story-a', 'wave-1', 'pending'), node('story-b', 'wave-2', 'passed')], edges: [],
};

async function installFixture(page: Page, flows: Record<string, typeof graph>) {
  await page.addInitScript(() => {
    if (sessionStorage.getItem('cognito_id_token')) return;
    const expiry = Date.now() + 3_600_000;
    const token = `${btoa('{}')}.${btoa(JSON.stringify({
      sub: 'fixture-reader', email: 'reader@example.invalid', 'custom:role': 'member',
      'custom:org_id': 'fixture-tenant', exp: Math.floor(expiry / 1000), auth_time: Math.floor(Date.now() / 1000),
    }))}.fixture`;
    sessionStorage.setItem('cognito_access_token', token);
    sessionStorage.setItem('cognito_id_token', token);
    sessionStorage.setItem('cognito_token_expiry', String(expiry));
  });
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    const requestedFlow = path.match(/\/orchestration\/flows\/([^/]+)$/)?.[1];
    if (requestedFlow && flows[requestedFlow]) return route.fulfill({ json: flows[requestedFlow] });
    if (path.endsWith('/execution')) return route.fulfill({ status: 503, json: { message: 'Fixture ledger unavailable' } });
    if (path.endsWith('/features')) return route.fulfill({ json: { features: { orchestration_engine: true } } });
    if (path.endsWith('/access/status')) return route.fulfill({ json: { status: 'registered' } });
    if (path.endsWith('/auth/workspaces')) return route.fulfill({ json: { items: [{ org_id: 'fixture-tenant', name: 'Fixture' }] } });
    if (path.endsWith('/auth/workflow-permissions')) return route.fulfill({ json: { org_id: 'fixture-tenant', permissions: [] } });
    return route.fulfill({ json: {} });
  });
}

test('test_graph_before_activity AC-01 MOD-018-F001-S001', async ({ page }) => {
  const started = { ...graph, nodes: [
    { ...graph.nodes[0], state: 'running', attempts: 1, run_id: 'current-run' }, graph.nodes[1],
  ] };
  await installFixture(page, { [flowId]: started });
  let requested!: (ids: string[]) => void;
  let release!: () => void;
  const activityRequested = new Promise<string[]>(resolve => { requested = resolve; });
  const activityReleased = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/orchestration/flows/${flowId}/activity*`, async route => {
    requested(new URL(route.request().url()).searchParams.getAll('node_ids'));
    await activityReleased;
    await route.fulfill({ json: { flow_id: flowId, observed_at: '2026-01-01T00:00:00Z', items: [
      { node_id: 'story-a', attempt: 1, run_id: 'current-run', availability: 'available', execution_history: {
        run_id: 'current-run', activity: null, runs: [], history_complete: false,
      } },
    ] } }).catch(() => {});
  });
  try {
    await page.goto(`/flows/${flowId}`);
    await expect(page.getByRole('heading', { name: graph.title })).toBeVisible({ timeout: 8_000 });
    expect(await activityRequested).toEqual(['story-a']);
    await expect(page.getByTestId('node-story-a')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Expand all waves' })).toBeVisible();
    await expect(page.getByTestId('activity-loading-story-a')).toBeVisible();
    await expect(page.getByTestId('graph-loading')).toHaveCount(0);
    await expect(page.getByTestId('node-story-b')).not.toBeVisible();
  } finally {
    release();
  }
});

test('test_workspace_scope_cancels_activity AC-02', async ({ page }) => {
  await installFixture(page, { [flowId]: graph });
  let currentCore = { ...graph, nodes: [
    { ...graph.nodes[0], state: 'running', attempts: 1, run_id: 'old-workspace-run' }, graph.nodes[1],
  ] };
  await page.route(`**/api/orchestration/flows/${flowId}?*`, route => route.fulfill({ json: currentCore }));
  let activityStarted!: () => void;
  let releaseActivity!: () => void;
  const requested = new Promise<void>(resolve => { activityStarted = resolve; });
  const released = new Promise<void>(resolve => { releaseActivity = resolve; });
  let activityCalls = 0;
  await page.route(`**/api/orchestration/flows/${flowId}/activity*`, async route => {
    activityCalls += 1;
    activityStarted();
    await released;
    await route.fulfill({ json: { flow_id: flowId, observed_at: '2026-01-01T00:00:00Z', items: [
      { node_id: 'story-a', attempt: 1, run_id: 'old-workspace-run', availability: 'available', execution_history: {
        run_id: 'old-workspace-run', activity: null, runs: [], history_complete: true,
      } },
    ] } }).catch(() => {});
  });
  try {
    await page.goto(`/flows/${flowId}`);
    await requested;
    currentCore = { ...graph, activity_included: true, title: 'Other workspace graph' };
    await page.evaluate(() => {
      const parts = sessionStorage.getItem('cognito_id_token')!.split('.');
      const claims = JSON.parse(atob(parts[1]));
      claims['custom:org_id'] = 'fixture-tenant-b';
      const updated = `${parts[0]}.${btoa(JSON.stringify(claims))}.${parts[2]}`;
      sessionStorage.setItem('cognito_access_token', updated);
      sessionStorage.setItem('cognito_id_token', updated);
    });
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Other workspace graph' })).toBeVisible();
    releaseActivity();
    await expect(page.locator('a[href*="old-workspace-run"]')).toHaveCount(0);
    expect(activityCalls).toBe(1);
  } finally {
    releaseActivity();
  }
});

test('test_navigation_cancels_stale_activity AC-02 MOD-018-F001-S005', async ({ page }) => {
  const flowB = 'fixture-flow-b';
  const flows = {
    [flowId]: { ...graph, nodes: [
      { ...graph.nodes[0], state: 'running', attempts: 1, run_id: 'old-run' }, graph.nodes[1],
    ] },
    [flowB]: { ...graph, flow_id: flowB, title: 'Other delivery graph', activity_included: true },
  };
  await installFixture(page, flows);
  let activityStarted!: () => void;
  let releaseActivity!: () => void;
  const requested = new Promise<void>(resolve => { activityStarted = resolve; });
  const released = new Promise<void>(resolve => { releaseActivity = resolve; });
  const cancelled: string[] = [];
  page.on('requestfailed', request => {
    if (request.url().includes('/activity?')) cancelled.push(request.url());
  });
  await page.route(`**/api/orchestration/flows/${flowId}/activity*`, async route => {
    activityStarted();
    await released;
    await route.fulfill({ json: { flow_id: flowId, observed_at: '2026-01-01T00:00:00Z', items: [
      { node_id: 'story-a', attempt: 1, run_id: 'old-run', availability: 'available', execution_history: {
        run_id: 'old-run', activity: null, runs: [], history_complete: true,
      } },
    ] } }).catch(() => {});
  });
  try {
    await page.goto(`/flows/${flowId}`);
    await requested;
    await page.evaluate((target) => {
      history.pushState({}, '', `/flows/${target}`);
      dispatchEvent(new PopStateEvent('popstate'));
    }, flowB);
    await expect(page.getByRole('heading', { name: 'Other delivery graph' })).toBeVisible();
    releaseActivity();
    await expect.poll(() => cancelled.length).toBeGreaterThan(0);
    await expect(page.getByRole('heading', { name: 'Other delivery graph' })).toBeVisible();
    await expect(page.locator('a[href*="old-run"]')).toHaveCount(0);
    await expect(page.getByTestId('graph-loading')).toHaveCount(0);
  } finally {
    releaseActivity();
  }
});

test('test_attempt_refresh_keeps_graph AC-02', async ({ page }) => {
  const flows = { [flowId]: { ...graph, nodes: [
    { ...graph.nodes[0], state: 'running', attempts: 1, run_id: 'old-run' }, graph.nodes[1],
  ] } };
  await installFixture(page, flows);
  let firstActivityStarted!: () => void;
  let releaseFirstActivity!: () => void;
  const requested = new Promise<void>(resolve => { firstActivityStarted = resolve; });
  const released = new Promise<void>(resolve => { releaseFirstActivity = resolve; });
  await page.route(`**/api/orchestration/flows/${flowId}/activity*`, async route => {
    const ids = new URL(route.request().url()).searchParams.getAll('node_ids');
    const { attempts, run_id: runId } = flows[flowId].nodes[0];
    if (attempts === 1) {
      firstActivityStarted();
      await released;
    }
    await route.fulfill({ json: { flow_id: flowId, observed_at: '2026-01-01T00:00:00Z', items: ids.map(id => ({
      node_id: id, attempt: attempts, run_id: runId,
      availability: 'available', execution_history: {
        run_id: runId, activity: null, runs: [], history_complete: false,
      },
    })) } }).catch(() => {});
  });
  let coreReadCount = 0;
  let releaseCore!: () => void;
  const coreReleased = new Promise<void>(resolve => { releaseCore = resolve; });
  await page.route(`**/api/orchestration/flows/${flowId}?*`, async route => {
    coreReadCount += 1;
    if (coreReadCount > 1) {
      await coreReleased;
    }
    await route.fulfill({ json: flows[flowId] }).catch(() => {});
  });
  try {
    await page.goto(`/flows/${flowId}`);
    await requested;
    flows[flowId] = { ...flows[flowId], nodes: [
      { ...flows[flowId].nodes[0], attempts: 2, run_id: 'new-run' }, graph.nodes[1],
    ] };
    await page.evaluate(() => dispatchEvent(new Event('visibilitychange')));
    await expect.poll(() => coreReadCount, { timeout: 5_000 }).toBeGreaterThan(1);
    await expect(page.getByRole('heading', { name: graph.title })).toBeVisible();
    await expect(page.getByTestId('graph-loading')).toHaveCount(0);
    const refreshed = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/orchestration/flows/${flowId}`));
    releaseCore();
    await refreshed;
    await expect(page.getByTestId('node-attempts-story-a')).toHaveText('2 attempts');
    await expect(page.locator('a[href*="old-run"]')).toHaveCount(0);
    releaseFirstActivity();
    await expect(page.locator('a[href*="old-run"]')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: graph.title })).toBeVisible();
  } finally {
    releaseFirstActivity();
    releaseCore();
  }
});

test('test_activity_error_and_legacy SCN-01 AC-03', async ({ page }) => {
  const flows = { [flowId]: { ...graph, nodes: [
    { ...graph.nodes[0], state: 'running', attempts: 1, run_id: 'current-run' }, graph.nodes[1],
  ] } };
  await installFixture(page, flows);
  let activityCalls = 0;
  await page.route(`**/api/orchestration/flows/${flowId}/activity*`, async route => {
    activityCalls += 1;
    if (activityCalls < 3) {
      await route.fulfill({ status: activityCalls === 1 ? 404 : 401, json: { message: 'Activity unavailable' } });
    } else if (activityCalls === 3) {
      await route.abort('timedout');
    } else if (activityCalls < 6) {
      await route.fulfill({ contentType: 'application/json', body: activityCalls === 4 ? 'null' : 'false' });
    } else {
      await route.fulfill({ json: { flow_id: flowId, observed_at: '2026-01-01T00:00:00Z', items: [
        { node_id: 'story-a', attempt: 1, run_id: 'current-run', availability: 'available', execution_history: {
          run_id: 'current-run', activity: null, runs: [], history_complete: true,
        } },
      ] } });
    }
  });
  await page.goto(`/flows/${flowId}`);
  await expect(page.getByTestId('activity-unavailable-story-a')).toBeVisible();
  await expect(page.getByRole('heading', { name: graph.title })).toBeVisible();
  await expect(page.getByTestId('node-story-a')).not.toHaveAttribute('data-display-state', 'complete');
  expect(activityCalls).toBe(1);

  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/orchestration/flows/${flowId}`));
  await page.evaluate(() => dispatchEvent(new Event('visibilitychange')));
  await refreshed;
  // A new core observation retries failed activity once. Wait for that retry
  // before testing manual retries; the response precedes React's state update.
  await expect.poll(() => activityCalls).toBe(2);
  await expect(page.getByTestId('activity-unavailable-story-a')).toBeVisible();
  for (const expectedCall of [3, 4, 5, 6]) {
    await page.getByRole('button', { name: 'Retry activity' }).click();
    await expect.poll(() => activityCalls).toBe(expectedCall);
    if (expectedCall < 6) {
      await expect(page.getByTestId('activity-unavailable-story-a')).toBeVisible();
      await expect(page.getByRole('heading', { name: graph.title })).toBeVisible();
      await expect(page).toHaveURL(new RegExp(`/flows/${flowId}$`));
    }
  }
  await expect(page.getByTestId('activity-unavailable-story-a')).toHaveCount(0);
  await expect(page.getByTestId('node-story-a')).not.toHaveAttribute('data-display-state', 'complete');

  flows[flowId] = { ...graph, activity_included: undefined, nodes: [
    { ...graph.nodes[0], state: 'running', attempts: 1, run_id: 'legacy-run',
      execution_history: { run_id: 'legacy-run', activity: null, runs: [], history_complete: false },
    }, graph.nodes[1],
  ] };
  await page.reload();
  await expect(page.locator('a[href*="legacy-run"]')).toBeVisible();
  expect(activityCalls).toBe(6);

  flows[flowId] = { ...flows[flowId], activity_included: true };
  await page.reload();
  await expect(page.locator('a[href*="legacy-run"]')).toBeVisible();
  expect(activityCalls).toBe(6);
});
