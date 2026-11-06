import { expect, test, type Page } from '@playwright/test';

const flowId = 'fixture-flow-a';
const graph = {
  flow_id: flowId,
  slug: flowId,
  title: 'Fixture delivery flow',
  intent_ref: null,
  state: 'pending',
  execution_paused: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: null,
  cost: { status: 'unknown', reason: 'no_usage_rows', node_count: 2, unknown_node_count: 2, partial: true },
  nodes: [
    { id: 'story-a', epic_ref: 'epic-1', wave_ref: 'wave-1', node_ref: 'story-a', kind: 'story',
      title: 'Build the feature', state: 'pending', stalled: false, issue_ref: null, attempts: 0,
      cost: { status: 'unknown', reason: 'not_started' } },
    { id: 'story-b', epic_ref: 'epic-1', wave_ref: 'wave-1', node_ref: 'story-b', kind: 'story',
      title: 'Verify the feature', state: 'pending', stalled: false, issue_ref: null, attempts: 0,
      cost: { status: 'unknown', reason: 'not_started' } },
  ],
  edges: [{ from_node_id: 'story-a', to_node_id: 'story-b' }],
};

async function installAuthAndRoutes(page: Page, core = graph, permissions: string[] = []) {
  await page.addInitScript(() => {
    const expiry = Date.now() + 3_600_000;
    const token = `${btoa('{}')}.${btoa(JSON.stringify({
      sub: 'fixture-reader', email: 'reader@example.invalid', 'custom:role': 'member',
      'custom:org_id': 'fixture-tenant-a', exp: Math.floor(expiry / 1000), auth_time: Math.floor(Date.now() / 1000),
    }))}.fixture`;
    sessionStorage.setItem('cognito_access_token', token);
    sessionStorage.setItem('cognito_id_token', token);
    sessionStorage.setItem('cognito_token_expiry', String(expiry));
  });
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/orchestration/flows/${flowId}`)) return route.fulfill({ json: core });
    if (path.endsWith('/execution')) return route.fulfill({ json: {
      flow_id: core.flow_id, server_time: '2026-01-01T00:00:00Z', legacy: true, executions: [],
    } });
    if (path.endsWith('/features')) return route.fulfill({ json: { features: { orchestration_engine: true } } });
    if (path.endsWith('/access/status')) return route.fulfill({ json: { status: 'registered' } });
    if (path.endsWith('/auth/workspaces')) {
      return route.fulfill({ json: { items: [{ org_id: 'fixture-tenant-a', name: 'Fixture workspace' }] } });
    }
    if (path.endsWith('/auth/workflow-permissions')) {
      return route.fulfill({ json: { org_id: 'fixture-tenant-a', permissions } });
    }
    return route.fulfill({ json: {} });
  });
}

async function meaningfulGraph(page: Page, coreResponse: Promise<unknown>, timeout: number): Promise<boolean> {
  try {
    await page.getByRole('heading', { name: graph.title, exact: true }).waitFor({ state: 'visible', timeout });
    await page.getByTestId('node-story-a').waitFor({ state: 'visible', timeout });
    await page.getByTestId('node-story-b').waitFor({ state: 'visible', timeout });
    await page.getByRole('button', { name: 'Expand all waves' }).waitFor({ state: 'visible', timeout });
    const body = await coreResponse as typeof graph;
    return body.flow_id === flowId && body.nodes.length === 2 && body.edges.some(
      edge => edge.from_node_id === 'story-a' && edge.to_node_id === 'story-b',
    );
  } catch {
    return false;
  }
}

for (const width of [1440, 390]) {
  test(`flow budget and lazy run balances fit a ${width}px viewport`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installAuthAndRoutes(page);
    await page.route(`**/api/orchestration/flows/${flowId}/budget`, route => route.fulfill({ json: {
      flow_id: flowId, observed_at: '2026-10-27T12:00:00Z', run_limit_usd: '100', concurrency: 5,
      enforcement_enabled: true, accounting_incomplete: false,
      balance: { limit_usd: '2000', spent_usd: '450.25', reserved_usd: '30.25', remaining_usd: '1519.50' },
    } }));
    let runReads = 0;
    await page.route(`**/api/orchestration/flows/${flowId}/nodes/story-a/budget`, route => {
      runReads++;
      return route.fulfill({ json: { flow_id: flowId, node_id: 'story-a', observed_at: '2026-10-27T12:00:00Z',
        enforcement_enabled: true, truncated: false, runs: [{ run_id: 'reviewer-one', persona: 'agent-codex-reviewer', last_call_at: '2026-10-27T12:00:00Z',
          balance: { limit_usd: '100', spent_usd: '41.22', reserved_usd: '8.33', remaining_usd: '50.45' } }],
      } });
    });
    await page.goto(`/flows/${flowId}`);
    const summary = page.getByRole('region', { name: 'Delivery flow budget' });
    for (const value of ['$2000.00', '$450.25', '$30.25', '$1519.50', '$100.00']) await expect(summary.getByText(value, { exact: true })).toBeVisible();
    expect(runReads).toBe(0);
    const story = page.getByTestId('node-story-a');
    await story.getByText('Run budgets', { exact: true }).click();
    await expect(story.getByRole('table', { name: 'Budget by agent run' }).getByText('$50.45')).toBeVisible();
    expect(runReads).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  });
}

test('meaningful core graph renders before delayed execution enrichment', async ({ page }) => {
  await installAuthAndRoutes(page);
  const coreResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/orchestration/flows/${flowId}`)).then(response => response.json());
  let received!: () => void;
  let release!: () => void;
  const enrichmentRequested = new Promise<void>(resolve => { received = resolve; });
  const enrichmentReleased = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/orchestration/flows/${flowId}/execution*`, async route => {
    received();
    await enrichmentReleased;
    await route.fulfill({ json: { flow_id: flowId, observed_at: '2026-01-01T00:00:00Z', executions: [] } });
  });
  try {
    await page.goto(`/flows/${flowId}`);
    await expect(page.getByRole('heading', { name: graph.title, exact: true })).toBeVisible({ timeout: 8_000 });
    await enrichmentRequested;
    expect(await meaningfulGraph(page, coreResponse, 5_000)).toBe(true);
    await expect(page.getByTestId('stage-epic-1-wave-1-0')).toBeVisible();
    await expect(page.getByTestId('stage-epic-1-wave-1-1')).toBeVisible();
  } finally {
    release();
  }
});

test('a missing core response does not count as meaningful graph content', async ({ page }) => {
  await installAuthAndRoutes(page);
  let received!: () => void;
  let release!: () => void;
  const coreRequested = new Promise<void>(resolve => { received = resolve; });
  const coreReleased = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/orchestration/flows/${flowId}?*`, async route => {
    received();
    await coreReleased;
    await route.fulfill({ json: graph });
  });
  try {
    await page.goto(`/flows/${flowId}`);
    await coreRequested;
    expect(await meaningfulGraph(page, Promise.resolve(graph), 300)).toBe(false);
    await expect(page.getByTestId('graph-loading')).toBeVisible();
  } finally {
    release();
  }
});

test('a foreign graph identity cannot satisfy the expected-content check', async ({ page }) => {
  await installAuthAndRoutes(page, { ...graph, flow_id: 'foreign-flow' });
  const coreResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(`/orchestration/flows/${flowId}`)).then(response => response.json());
  await page.goto(`/flows/${flowId}`);
  expect(await meaningfulGraph(page, coreResponse, 5_000)).toBe(false);
});

for (const route of ['/flows', `/flows/${flowId}`]) {
  test(`${route} renders while membership discovery is still pending`, async ({ page }) => {
    await installAuthAndRoutes(page);
    await page.route('**/api/orchestration/flows?*', request => request.fulfill({ json: {
      flows: [{ id: flowId, slug: flowId, title: graph.title, status: 'queued', total_nodes: 2,
        waves: [], wave_count: 0, current_wave_ref: null,
        display_counts: { queued: 2, in_progress: 0, gate: 0, stalled: 0, complete: 0 },
        delivery_cost: { status: 'unknown', reason: 'no_usage_rows' } }],
      total: 1, limit: 25, offset: 0, status_counts: { queued: 1 },
    } }));
    let fastReads = 0;
    let syncStarted!: () => void;
    let release!: () => void;
    const started = new Promise<void>(resolve => { syncStarted = resolve; });
    const discovery = new Promise<void>(resolve => { release = resolve; });
    await page.route('**/api/access/status*', async request => {
      if (new URL(request.request().url()).searchParams.get('sync_memberships') === 'false') {
        fastReads++;
      } else {
        syncStarted();
        await discovery;
      }
      await request.fulfill({ json: { status: 'registered' } }).catch(() => {});
    });
    try {
      await page.goto(route);
      await expect(page.getByTestId(route === '/flows' ? `flow-card-${flowId}` : 'node-story-a')).toBeVisible();
      await started;
      expect(fastReads).toBe(1);
      await expect(page.getByRole('combobox', { name: 'Organization' })).toBeEnabled();
    } finally { release(); }
  });
}

test('unopened waves do not mount story components', async ({ page }) => {
  const core = { ...graph, nodes: Array.from({ length: 80 }, (_, i) => ({
    ...graph.nodes[0], id: `story-${i}`, node_ref: `story-${i}`, wave_ref: `wave-${i}`,
  })), edges: [] };
  await installAuthAndRoutes(page, core);
  await page.goto(`/flows/${flowId}`);
  await expect(page.getByTestId('node-story-0')).toBeVisible();
  await expect(page.locator('[data-container="wave"]')).toHaveCount(80);
  await expect(page.locator('[data-node-kind]')).toHaveCount(1);
  await page.getByRole('button', { name: 'Expand all waves' }).click();
  await expect(page.locator('[data-node-kind]')).toHaveCount(80);
});


for (const width of [1440, 390]) {
  test(`flow views preserve review notes and fit a ${width}px viewport`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const gate = { ...graph.nodes[0], id: 'gate', node_ref: 'gate', kind: 'gate', title: 'Approve delivery', state: 'awaiting_gate' };
    const laterSteps = Array.from({ length: 12 }, (_, i) => ({ ...graph.nodes[1], id: `later-${i}`, node_ref: `later-${i}`, title: `Later step ${i}`, wave_ref: `wave-${i + 2}` }));
    const core = { ...graph, nodes: [{ ...graph.nodes[0], state: 'passed', attempts: 2 }, graph.nodes[1], gate, ...laterSteps], edges: [] };
    await installAuthAndRoutes(page, core, ['plan:approve']);
    await page.route('**/api/orchestration/flows/*/plans', route => route.fulfill({ json: [{
      version: 1, plan_hash: 'a'.repeat(64), superseded_at: null,
      plan_document: { title: 'Reviewed delivery', nodes: [{ address: `${flowId}/epic-1/wave-1/gate`, title: gate.title, kind: 'gate' }] },
    }] }));
    await page.route('**/api/orchestration/gates/*/execution-preview*', route => route.fulfill({ json: { required: false, ready: true, runs: [], problems: [] } }));
    await page.route('**/api/orchestration/flows/*/amendments/drafts', route => route.fulfill({ json: [{
      draft_id: 'draft-fixture', flow_id: flowId, state: 'pending', request_text: 'Detailed change request '.repeat(100), proposal_hash: 'b'.repeat(64), base_is_current: true,
    }] }));
    let timelineReads = 0;
    await page.route('**/api/orchestration/flows/*/timeline*', route => {
      timelineReads++;
      return route.fulfill({ json: { flow_id: flowId, server_time: '2026-01-01T12:00:00Z', total: 2 + laterSteps.length, limit: 100, offset: 0, nodes: [
        { node_id: 'story-a', state: 'passed', attempts: 2, started_at: '2026-01-01T10:00:00Z', finished_at: '2026-01-01T11:00:00Z', transitions: [], transitions_truncated: false },
        { node_id: 'story-b', state: 'pending', attempts: 0, started_at: null, finished_at: null, transitions: [], transitions_truncated: false },
        ...laterSteps.map(node => ({ node_id: node.id, state: 'pending', attempts: 0, started_at: null, finished_at: null, transitions: [], transitions_truncated: false })),
      ] } });
    });
    await page.goto(`/flows/${flowId}`);
    const approve = page.getByTestId('gate-approve');
    await expect(approve).toBeEnabled();
    expect(timelineReads).toBe(0);
    await page.getByTestId('gate-controls-reason').fill('Keep this note while comparing story timings');
    await page.getByRole('tab', { name: 'Timeline', exact: true }).click();
    await expect(page.getByTestId('timeline-story-a')).toContainText('1h 0m');
    await expect(page.getByTestId('timeline-story-a')).toContainText('2 attempts');
    if (width === 390) expect((await page.getByTestId('timeline-story-a').getByRole('button').boundingBox())!.width).toBeGreaterThan(200);
    await expect(page.getByTestId('timeline-story-b')).toContainText('Timing unavailable');
    expect(timelineReads).toBe(1);
    await page.screenshot({ path: test.info().outputPath('timeline.png'), fullPage: true });
    await page.getByRole('tab', { name: /Plan changes/ }).click();
    await expect(page.getByRole('combobox', { name: 'Pending amendment' })).toBeVisible();
    await expect(page.getByRole('tab', { name: /Plan changes/ })).toContainText('(1)');
    await page.screenshot({ path: test.info().outputPath('plan-changes.png'), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole('tab', { name: 'Overview', exact: true }).click();
    await expect(page.getByTestId('gate-controls-reason')).toHaveValue('Keep this note while comparing story timings');
    await page.getByRole('link', { name: 'Approve delivery', exact: true }).click();
    await expect(page.getByTestId('node-gate')).toBeFocused();
    const gateTop = await page.getByTestId('node-gate').evaluate(element => element.getBoundingClientRect().top);
    const headerBottom = await page.locator('header.blueprint-header').evaluate(element => element.getBoundingClientRect().bottom);
    expect(gateTop).toBeGreaterThanOrEqual(headerBottom + 8);
    await page.screenshot({ path: test.info().outputPath('decision.png'), fullPage: true });
    await page.getByRole('tab', { name: 'Overview', exact: true }).focus();
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab', { name: 'Timeline', exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test('compact list grows completed progress from the left and preserves filters on return', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await installAuthAndRoutes(page);
  await page.route('**/api/orchestration/flows?*', route => route.fulfill({ json: {
    flows: Array.from({ length: 4 }, (_, i) => ({ id: i === 0 ? flowId : `fixture-${i}`, slug: `fixture-${i}`, title: `Delivery ${i + 1}`, status: 'running',
      story_count: 10, completed_story_count: 4, eval_story_count: 0, completed_eval_story_count: 0,
      total_nodes: 10, waves: [], wave_count: 0, current_wave_ref: null,
      display_counts: { queued: 5, in_progress: 1, gate: 0, stalled: 0, complete: 4 },
      delivery_cost: { status: 'unknown', reason: 'no_usage_rows' } })), total: 4, limit: 25, offset: 0, status_counts: { running: 4 },
  } }));
  await page.goto('/flows?status=running&q=Delivery');
  const first = page.getByTestId(`flow-card-${flowId}`);
  await expect(first).toBeVisible();
  const progress = first.getByRole('progressbar');
  const bounds = await progress.boundingBox();
  const fill = await first.getByTestId('story-progress-fill').boundingBox();
  expect(fill!.x).toBeCloseTo(bounds!.x, 1);
  expect(fill!.width / bounds!.width).toBeCloseTo(0.4, 2);
  expect((await page.getByTestId('flow-card-fixture-2').boundingBox())!.y).toBeLessThan(1000);
  await page.screenshot({ path: test.info().outputPath('flow-list.png'), fullPage: true });
  await first.click();
  await page.getByRole('link', { name: '← Delivery flows', exact: true }).click();
  await expect(page).toHaveURL(/status=running&q=Delivery/);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
