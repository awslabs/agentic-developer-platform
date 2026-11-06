import { test, expect, type Browser, type Page } from '@playwright/test';
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { waitForVisibleFlowNodes } from './flow-read-observer';

type Config = {
  frontend_url: string;
  auth_file: string;
  list_flow_ids: string[];
  list_query?: string;
  graph_flow_id: string;
  graph_title: string;
  graph_node_ids: string[];
  graph_node_refs: string[];
  graph_edges: [string, string][];
};
type Attempt = {
  operation: 'list' | 'graph'; reader: number; sample: number; classification: string;
  meaningful_seconds: number | null; core_http_seconds: number | null;
  enrichment_seconds: number | null; status: number | null; api_attempts: number;
  warm_polls: number;
};

const configPath = process.env.FLOW_READ_CONFIG;
const captureDir = process.env.FLOW_READ_CAPTURE_DIR;
if (!configPath || !captureDir) throw new Error('FLOW_READ_CONFIG and FLOW_READ_CAPTURE_DIR are required');
const config = JSON.parse(readFileSync(configPath, 'utf8')) as Config;
const auth = JSON.parse(readFileSync(config.auth_file, 'utf8')) as { access_token: string; id_token: string; expiry_ms: number };
if (!config.frontend_url || !auth.access_token || !auth.id_token || auth.expiry_ms <= Date.now()) {
  throw new Error('Frontend address and current private auth file are required');
}
const attemptLimit = Number(process.env.FLOW_READ_REMAINING_ATTEMPTS ?? '3000');
const activeSeconds = Number(process.env.FLOW_READ_REMAINING_SECONDS ?? '1200');
if (!Number.isSafeInteger(attemptLimit) || attemptLimit < 0 || !Number.isFinite(activeSeconds) || activeSeconds <= 0) {
  throw new Error('Invalid shared run limits');
}
const deadline = Date.now() + activeSeconds * 1000;
if (process.env.FLOW_READ_BUDGET_LOG && readFileSync(process.env.FLOW_READ_BUDGET_LOG).length !== 0) {
  throw new Error('Browser worker restarted during an owned run');
}
let reserved = 0;
let consecutiveFailures = 0;
let stopped = false;

function contentMatches(operation: Attempt['operation'], body: unknown): boolean {
  if (!body || typeof body !== 'object') return false;
  const result = body as { flows?: { id: string }[]; flow_id?: string; nodes?: { id: string }[]; edges?: { from_node_id: string; to_node_id: string }[] };
  if (operation === 'list') {
    return Array.isArray(result.flows) && result.flows.length === config.list_flow_ids.length
      && result.flows.every(flow => config.list_flow_ids.includes(flow.id));
  }
  return result.flow_id === config.graph_flow_id && Array.isArray(result.nodes)
    && result.nodes.length === config.graph_node_ids.length
    && result.nodes.every(node => config.graph_node_ids.includes(node.id))
    && Array.isArray(result.edges) && result.edges.length === config.graph_edges.length
    && result.edges.every(edge => config.graph_edges.some(([source, target]) => edge.from_node_id === source && edge.to_node_id === target));
}

async function meaningful(page: Page, operation: Attempt['operation']): Promise<void> {
  if (operation === 'list') {
    await page.getByRole('heading', { name: 'Delivery Flows', exact: true }).waitFor({ state: 'visible', timeout: 3_000 });
    for (const flowId of config.list_flow_ids) {
      await page.getByTestId(`flow-card-${flowId}`).waitFor({ state: 'visible', timeout: 3_000 });
    }
    return;
  }
  await page.getByRole('heading', { name: config.graph_title, exact: true }).waitFor({ state: 'visible', timeout: 3_000 });
  await page.getByRole('button', { name: 'Expand all waves' }).click({ timeout: 3_000 });
  await waitForVisibleFlowNodes(page, config.graph_node_refs);
}

async function navigate(browser: Browser, operation: Attempt['operation'], reader: number, sample: number): Promise<Attempt> {
  const context = await browser.newContext({ baseURL: config.frontend_url });
  await context.addInitScript(({ access, id, expiry }) => {
    sessionStorage.setItem('cognito_access_token', access);
    sessionStorage.setItem('cognito_id_token', id);
    sessionStorage.setItem('cognito_token_expiry', String(expiry));
  }, { access: auth.access_token, id: auth.id_token, expiry: auth.expiry_ms });
  const page = await context.newPage();
  if (process.env.FLOW_READ_MOCK === '1') {
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/features')) return route.fulfill({ json: { features: { orchestration_engine: true } } });
      if (path.endsWith('/access/status')) return route.fulfill({ json: { status: 'registered' } });
      if (path.endsWith('/auth/workspaces')) {
        return route.fulfill({ json: { items: [{ org_id: 'fixture-tenant-a', name: 'Fixture workspace' }] } });
      }
      if (path.endsWith('/auth/workflow-permissions')) {
        return route.fulfill({ json: { org_id: 'fixture-tenant-a', permissions: [] } });
      }
      if (path.endsWith('/orchestration/flows')) {
        expect(new URL(route.request().url()).searchParams.get('q') ?? '').toBe(config.list_query ?? 'fixture-flow');
        expect(new URL(route.request().url()).searchParams.get('limit')).toBe('25');
        const flows = config.list_flow_ids.map(id => ({
          id, slug: id, title: 'Fixture flow', status: 'queued', total_nodes: 2,
          waves: [], wave_count: 0, current_wave_ref: null,
          display_counts: { queued: 2, in_progress: 0, gate: 0, stalled: 0, complete: 0 },
          delivery_cost: { status: 'unknown', reason: 'no_usage_rows' },
        }));
        return route.fulfill({ json: { flows, total: flows.length, limit: 25, offset: 0, status_counts: { queued: flows.length } } });
      }
      if (path.endsWith(`/orchestration/flows/${config.graph_flow_id}`)) {
        const nodes = config.graph_node_ids.map((id, index) => ({
          id, epic_ref: 'epic-1', wave_ref: 'wave-1', node_ref: config.graph_node_refs[index],
          kind: 'story', title: `Story ${index}`, state: 'pending', stalled: false, issue_ref: null, attempts: 0,
          cost: { status: 'unknown', reason: 'not_started' },
        }));
        return route.fulfill({ json: {
          flow_id: config.graph_flow_id, slug: config.graph_flow_id, title: config.graph_title,
          intent_ref: null, state: 'pending', execution_paused: true, created_at: '2026-01-01T00:00:00Z',
          nodes, edges: config.graph_edges.map(([from_node_id, to_node_id]) => ({ from_node_id, to_node_id })),
          cost: { status: 'unknown', reason: 'no_usage_rows', node_count: nodes.length, unknown_node_count: nodes.length },
        } });
      }
      if (path.endsWith('/execution')) {
        return route.fulfill({ json: { flow_id: config.graph_flow_id, observed_at: '2026-01-01T00:00:00Z', executions: [] } });
      }
      return route.fulfill({ json: {} });
    });
  }
  await page.route('**/api/**', route => {
    if (reserved >= attemptLimit || stopped || Date.now() >= deadline) {
      stopped = true;
      return route.abort();
    }
    reserved++;
    if (process.env.FLOW_READ_BUDGET_LOG) appendFileSync(process.env.FLOW_READ_BUDGET_LOG, '1\n');
    return route.fallback();
  });
  let apiAttempts = 0;
  let coreRequests = 0;
  let coreHttpSeconds: number | null = null;
  let enrichmentSeconds: number | null = null;
  let status: number | null = null;
  let unauthorized = false;
  let coreBody: Promise<unknown> | null = null;
  let started = 0;
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) apiAttempts++;
    const path = new URL(request.url()).pathname;
    if (path.endsWith(`/orchestration/flows/${config.graph_flow_id}`) || path.endsWith('/orchestration/flows')) coreRequests++;
  });
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (response.status() === 401 || response.status() === 403) unauthorized = true;
    const corePath = operation === 'list' ? path.endsWith('/orchestration/flows') : path.endsWith(`/orchestration/flows/${config.graph_flow_id}`);
    if (corePath) {
      status = response.status();
      coreHttpSeconds = (performance.now() - started) / 1_000;
      coreBody = response.json().catch(() => null);
    }
    if (path.endsWith('/execution') || path.includes('/activity')) enrichmentSeconds = (performance.now() - started) / 1_000;
  });
  let classification = 'ok';
  let meaningfulSeconds: number | null = null;
  try {
    const address = operation === 'list' ? `/flows?q=${encodeURIComponent(config.list_query ?? 'fixture-flow')}` : `/flows/${encodeURIComponent(config.graph_flow_id)}`;
    started = performance.now();
    await page.goto(address, { waitUntil: 'domcontentloaded', timeout: Math.min(15_000, Math.max(1, deadline - Date.now())) });
    await meaningful(page, operation);
    meaningfulSeconds = (performance.now() - started) / 1_000;
    if (unauthorized) classification = 'auth_violation';
    else if (!coreBody || !contentMatches(operation, await coreBody)) classification = 'content_violation';
  } catch {
    classification = unauthorized ? 'auth_violation' : 'missing_content';
  } finally {
    await context.close();
  }
  consecutiveFailures = classification === 'ok' ? 0 : consecutiveFailures + 1;
  if (consecutiveFailures >= 5) stopped = true;
  return { operation, reader, sample, classification, meaningful_seconds: meaningfulSeconds,
    core_http_seconds: coreHttpSeconds, enrichment_seconds: enrichmentSeconds, status,
    api_attempts: apiAttempts, warm_polls: Math.max(0, coreRequests - 1) };
}

for (const readers of [1, 5]) {
  test(`${readers}-reader browser samples`, async ({ browser }) => {
    const run = async (reader: number): Promise<Attempt[]> => {
      const samples: Attempt[] = [];
      for (let sample = reader + 1; sample <= 20; sample += readers) {
        for (const operation of ['list', 'graph'] as const) {
          if (stopped || Date.now() >= deadline) return samples;
          const attempt = await navigate(browser, operation, reader, sample);
          // Preserve completed samples if a runtime guard interrupts the suite.
          appendFileSync(join(captureDir, `browser-${readers}.jsonl`), JSON.stringify(attempt) + '\n', { mode: 0o600 });
          samples.push(attempt);
        }
      }
      return samples;
    };
    const groups = await Promise.all(Array.from({ length: readers }, (_, reader) => run(reader)));
    const attempts = groups.flat();
    writeFileSync(join(captureDir, `browser-${readers}.json`), JSON.stringify({ readers, planned_per_operation: 20, attempts }));
    expect(attempts).toHaveLength(40);
    expect(attempts.every(attempt => attempt.classification === 'ok' && attempt.meaningful_seconds !== null)).toBe(true);
  });
}
