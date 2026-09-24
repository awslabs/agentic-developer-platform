/**
 * Playwright E2E scenario for live run controls — Issue #3966 (S7).
 *
 * Usage:
 *   npx playwright test --config tests/e2e/agent-control.config.ts
 *
 * Requires:
 *   - A frontend to drive (GATEWAY_URL, default http://localhost:5173)
 *   - `npm install -D @playwright/test && npx playwright install chromium`
 *     (@playwright/test is deliberately NOT a dependency of this package — see
 *     the config file for why)
 *
 * ---------------------------------------------------------------------------
 * WHAT EACH MODE PROVES — read this before citing a green run as evidence
 * ---------------------------------------------------------------------------
 *
 * This spec runs in one of two modes, and they are not interchangeable:
 *
 * **Mocked mode (default).** The gateway's control endpoints are fulfilled by
 * `page.route`. A pass proves the real browser renders the real bundle, that the
 * documented clicks reach the documented requests, that the honest wording
 * appears in the DOM, and that no pod address or token is ever addressed. It
 * does NOT prove a worker paused, that an agent received an instruction, or that
 * a run finished. Those claims require a real worker.
 *
 * **Live mode (`CONTROL_E2E_LIVE=1`).** Drives a deployed gateway against real
 * runs supplied through env vars. This is the mode that can produce Wave 4
 * acceptance evidence. It is gated on prerequisites that are NOT yet met on
 * main: the gateway does not route `steer` until S6 (#3965) lands, and live
 * acceptance additionally follows accepted Wave 3 (#3969). Live mode therefore
 * FAILS LOUDLY when its prerequisites are absent rather than skipping quietly —
 * a skipped control check reads as "covered" in a report, which is exactly the
 * false-confidence failure this story exists to prevent.
 */

import { test, expect, type Page, type Route } from '@playwright/test';
import { readFileSync } from 'node:fs';

// Generated from src.activity.schemas.InvocationItem, including nullable defaults.
const invocationTemplate = JSON.parse(readFileSync(new URL('./fixtures/agent-control-invocation.json', import.meta.url), 'utf8'));

const BASE_URL = process.env.GATEWAY_URL || 'http://localhost:5173';
const LIVE = process.env.CONTROL_E2E_LIVE === '1';

/** Runs supplied for live mode. Unused in mocked mode. */
const LIVE_PAUSE_RUN = process.env.CONTROL_E2E_RUN_ID || '';
const LIVE_ABORT_RUN = process.env.CONTROL_E2E_ABORT_RUN_ID || '';

/** Pod coordinates that must never appear in browser traffic. */
const FORBIDDEN_HOST = '10.0.42.7';
const FORBIDDEN_TOKEN = 'pod-bearer-token-must-never-leave-the-gateway';

// ---------------------------------------------------------------------------
// Auth — mirrors tests/e2e/knowledge.spec.ts
// ---------------------------------------------------------------------------

async function injectAuth(page: Page) {
  if (LIVE) {
    const file = process.env.CONTROL_E2E_SESSION_FILE;
    if (!file) throw new Error('Live browser acceptance requires CONTROL_E2E_SESSION_FILE with a real fixture user session');
    const session = JSON.parse(readFileSync(file, 'utf8'));
    if (typeof session.access_token !== 'string' || typeof session.id_token !== 'string'
        || typeof session.expires_at_ms !== 'number' || session.expires_at_ms <= Date.now()) {
      throw new Error('Live fixture session is missing tokens or has expired');
    }
    await page.addInitScript((tokens) => {
      window.sessionStorage.setItem('cognito_access_token', tokens.access_token);
      window.sessionStorage.setItem('cognito_id_token', tokens.id_token);
      window.sessionStorage.setItem('cognito_token_expiry', String(tokens.expires_at_ms));
      if (tokens.refresh_token) window.sessionStorage.setItem('cognito_refresh_token', tokens.refresh_token);
    }, session);
    return;
  }
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
        auth_time: Math.floor(Date.now() / 1000),
      }),
    );
    const token = `${header}.${payload}.${btoa('fake-signature')}`;
    window.sessionStorage.setItem('cognito_access_token', token);
    window.sessionStorage.setItem('cognito_id_token', token);
    window.sessionStorage.setItem('cognito_refresh_token', 'fake-refresh');
    window.sessionStorage.setItem('cognito_token_expiry', String(expiry));
  });
}

// ---------------------------------------------------------------------------
// Destination guard
// ---------------------------------------------------------------------------

/**
 * Record every URL the page requests and fail on any pod destination.
 *
 * The assertion is deliberately not "the app does not contain that string": it
 * watches actual network intent, so a dynamically supplied target — one injected
 * through a mocked response body below — is rejected too.
 */
function watchDestinations(page: Page): string[] {
  const urls: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    urls.push(url);
    const body = request.postData() || '';
    if ([url, body].some((value) => value.includes(FORBIDDEN_HOST) || value.includes(FORBIDDEN_TOKEN))) {
      throw new Error('Browser request exposed a pod destination or credential');
    }
  });
  return urls;
}

/** Console/page errors are failures: a crashed dashboard cannot be trusted. */
function watchConsole(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));
  return errors;
}

// ---------------------------------------------------------------------------
// Mocked gateway
// ---------------------------------------------------------------------------

const RUN_ID = 'run-e2e-live';
const ABORT_RUN_ID = 'run-e2e-abort';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

interface MockRun {
  state: string;
  capabilities: Record<string, boolean>;
  commands: Array<Record<string, unknown>>;
  activeTools: number | null;
  status: string;
  summary: string;
}

/**
 * A mutable in-memory worker.
 *
 * Commands mutate this the way a real worker would: pause moves to
 * `pause_requested` then `paused`, a steer is `pending` then `delivered`, and
 * the run's own summary changes only after the steer is delivered. That last
 * part is what makes "observe the expected steered change" meaningful rather
 * than a fixed string the test would have found either way.
 */
function createMockWorld() {
  const runs: Record<string, MockRun> = {
    [RUN_ID]: {
      state: 'running',
      capabilities: { pause: true, resume: true, steer: true, abort: true },
      commands: [],
      activeTools: 1,
      status: 'in_progress',
      summary: 'Working on the original approach',
    },
    [ABORT_RUN_ID]: {
      state: 'running',
      capabilities: { pause: true, resume: true, steer: false, abort: true },
      commands: [],
      activeTools: 0,
      status: 'in_progress',
      summary: 'A run that will be aborted',
    },
  };
  /** Polls remaining before a requested transition settles. */
  const pending: Record<string, number> = {};
  return { runs, pending };
}

async function mockGateway(page: Page, world: ReturnType<typeof createMockWorld>) {
  const { runs, pending } = world;
  await page.route('**/api/access/status', (route) => json(route, { status: 'registered' }));
  await page.route('**/api/auth/workspaces', (route) => json(route, { items: [] }));

  await page.route('**/api/features', (route) =>
    json(route, {
      features: {
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: false,
        orchestration_engine: false,
        budget_spend: false,
        // The flag under test. Everything below is unreachable without it.
        agent_control: true,
        new_ui: false,
        superplane: false,
        agent_models: false,
      },
    }),
  );

  const invocationBody = (id: string) => ({
    ...invocationTemplate,
    invocation_id: id,
    correlation_id: `corr-${id}`,
    status: runs[id].status,
    channel: 'manual',
    persona: 'developer',
    summary: runs[id].summary,
    invoked_at: '2026-09-24T10:00:00Z',
    status_updated_at: '2026-09-24T10:05:00Z',
    liveness: runs[id].state === 'terminal' ? 'exited' : 'live',
  });

  // Control state — the polled read contract.
  await page.route('**/api/activity/invocations/*/agent/state', (route) => {
    const match = /invocations\/([^/]+)\/agent/.exec(route.request().url());
    const id = decodeURIComponent(match?.[1] ?? '');
    const run = runs[id];
    if (!run) return json(route, { detail: 'run not found' }, 404);

    // Settle a requested transition after a poll, so the UI genuinely observes
    // `pause_requested` before `paused` rather than jumping straight to it.
    if (pending[id] !== undefined) {
      pending[id] -= 1;
      if (pending[id] <= 0) {
        delete pending[id];
        if (run.state === 'pause_requested') run.state = 'paused';
        else if (run.state === 'abort_requested') {
          run.state = 'terminal';
          run.status = 'aborted';
          run.capabilities = { pause: false, resume: false, steer: false, abort: false };
        }
        run.commands = run.commands.map((command) =>
          command.status === 'pending' ? { ...command, status: 'delivered' } : command,
        );
        // The steered change becomes visible only once the steer is delivered.
        if (run.commands.some((c) => c.action === 'steer' && c.status === 'delivered')) {
          run.summary = 'Switched to the steered approach';
        }
      }
    }

    return json(route, {
      run_id: id,
      generation: 1,
      available: run.state !== 'terminal',
      reason: run.state === 'terminal' ? 'run has finished' : null,
      capabilities: run.capabilities,
      state: run.state,
      active_tool_count: run.activeTools,
      updated_at: '2026-09-24T10:05:00Z',
      commands: run.commands,
      // A hostile/buggy worker offering a direct target. The destination watcher
      // asserts the browser never acts on it.
      control_address: FORBIDDEN_HOST,
      control_token: FORBIDDEN_TOKEN,
    });
  });

  // Commands.
  await page.route(/\/api\/activity\/invocations\/[^/]+\/agent\/(pause|resume|steer|abort)$/, (route) => {
    const url = route.request().url();
    const match = /invocations\/([^/]+)\/agent\/(pause|resume|steer|abort)/.exec(url);
    const id = decodeURIComponent(match?.[1] ?? '');
    const action = match?.[2] ?? '';
    const run = runs[id];
    if (!run) return json(route, { detail: 'run not found' }, 404);

    const body = JSON.parse(route.request().postData() || '{}');
    const commandId = body.command_id as string;

    if (action === 'pause') {
      run.state = 'pause_requested';
      pending[id] = 2;
    } else if (action === 'resume') {
      run.state = 'running';
    } else if (action === 'abort') {
      run.state = 'abort_requested';
      pending[id] = 2;
    } else if (action === 'steer') {
      pending[id] = 2;
    }

    run.commands = [...run.commands, { command_id: commandId, action, status: 'pending' }];
    return json(
      route,
      {
        run_id: id,
        action,
        state: run.state,
        command_id: commandId,
        command_status: 'pending',
      },
      202,
    );
  });

  await page.route('**/api/me/agent-invocations/*', (route) => {
    const match = /agent-invocations\/([^/?]+)/.exec(route.request().url());
    const id = decodeURIComponent(match?.[1] ?? '');
    if (!runs[id]) return json(route, { detail: 'not found' }, 404);
    return json(route, invocationBody(id));
  });

  await page.route('**/api/me/agent-invocations*', (route) =>
    json(route, { items: Object.keys(runs).map(invocationBody), last_key: null }),
  );

  // Unmatched requests continue normally; a final catch-all would shadow all
  // earlier handlers because Playwright evaluates routes in reverse order.
}

// ---------------------------------------------------------------------------
// Scenario
// ---------------------------------------------------------------------------

test.describe('live run controls', () => {
  test.beforeEach(async () => {
    if (LIVE) {
      // Fail, never skip: an unconfigured live run must not be reported as a
      // passed or skipped control check.
      expect(
        LIVE_PAUSE_RUN,
        'CONTROL_E2E_RUN_ID must name a live controllable run in live mode',
      ).not.toBe('');
      expect(
        LIVE_ABORT_RUN,
        'CONTROL_E2E_ABORT_RUN_ID must name a second live run in live mode',
      ).not.toBe('');
    }
  });

  test('pause, steer and resume a run, then observe the steered change', async ({ page }) => {
    const world = createMockWorld();
    const runId = LIVE ? LIVE_PAUSE_RUN : RUN_ID;
    const destinations = watchDestinations(page);
    const consoleErrors = watchConsole(page);

    await injectAuth(page);
    if (!LIVE) await mockGateway(page, world);

    // The existing deep link, not a new route.
    await page.goto(`${BASE_URL}/activity?id=${encodeURIComponent(runId)}`);

    const panel = page.getByRole('region', { name: /live run controls/i });
    await expect(panel).toBeVisible({ timeout: 15_000 });

    // ---- pause -----------------------------------------------------------
    const pauseRequest = page.waitForRequest(
      (request) =>
        request.url().includes(`/agent/pause`) && request.method() === 'POST',
    );
    await panel.getByRole('button', { name: /^pause$/i }).click();
    const sentPause = await pauseRequest;

    // The command carries a UUID idempotency key and no attribution fields.
    const pauseBody = JSON.parse(sentPause.postData() || '{}');
    expect(pauseBody.command_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(pauseBody).not.toHaveProperty('actor');
    expect(pauseBody).not.toHaveProperty('token');
    expect(pauseBody).not.toHaveProperty('target');

    // Pause is reported as requested — not as paused — and says spend continues.
    await expect(panel.getByTestId('control-phase')).toHaveText(/pause requested/i);
    await expect(panel.getByText(/spend may continue/i)).toBeVisible();

    // Then the tool-side-effect evidence: the run actually settles to paused.
    await expect(panel.getByTestId('control-phase')).toHaveText(/^Paused$/, {
      timeout: 15_000,
    });

    // ---- steer -----------------------------------------------------------
    const steerBox = panel.getByLabel(/send an instruction/i);
    if (LIVE) {
      // Before S6 (#3965) the gateway does not route steer, so its capability is
      // false and this control is correctly absent. Asserting its presence here
      // is the check that must not be quietly skipped when the prerequisite is
      // unmet — it should fail until steering is integrated.
      await expect(
        steerBox,
        'steer requires S6 (#3965) to be merged and its capability enabled',
      ).toBeVisible({ timeout: 10_000 });
    }
    await expect(steerBox).toBeVisible();
    await steerBox.fill('switch to the steered approach');

    const steerRequest = page.waitForRequest(
      (request) => request.url().includes('/agent/steer') && request.method() === 'POST',
    );
    await panel.getByRole('button', { name: /send instruction/i }).click();
    const sentSteer = await steerRequest;
    expect(JSON.parse(sentSteer.postData() || '{}').instruction).toBe(
      'switch to the steered approach',
    );

    // Queued, and described as queued — never as understood or acted on.
    const journal = panel.getByTestId('command-journal');
    await expect(journal).toContainText(/queued|handed to the agent/i);
    await expect(journal).not.toContainText(/understood|comprehend/i);

    // ---- resume ----------------------------------------------------------
    const resumeRequest = page.waitForRequest(
      (request) => request.url().includes('/agent/resume') && request.method() === 'POST',
    );
    await panel.getByRole('button', { name: /^resume$/i }).click();
    await resumeRequest;
    await expect(panel.getByTestId('control-phase')).toHaveText(/running/i, {
      timeout: 15_000,
    });

    // ---- the steered change ---------------------------------------------
    // Delivery evidence, then the observable consequence of the instruction.
    await expect(journal).toContainText(/handed to the agent/i, { timeout: 20_000 });
    await expect(page.getByRole('dialog').locator('dt').filter({ hasText: /^Summary$/ }).locator('..').locator('dd')).toContainText(/steered approach/i, {
      timeout: 20_000,
    });

    // ---- hygiene ---------------------------------------------------------
    expect(destinations.some((url) => url.includes(FORBIDDEN_HOST))).toBe(false);
    expect(destinations.some((url) => url.includes(FORBIDDEN_TOKEN))).toBe(false);
    // No pod credential may reach anything rendered or logged.
    expect(await page.content()).not.toContain(FORBIDDEN_TOKEN);
    expect(consoleErrors.join('\n')).not.toContain(FORBIDDEN_TOKEN);
    expect(consoleErrors.filter((line) => /control|agent/i.test(line))).toEqual([]);
  });

  test('abort a second run and observe its terminal evidence', async ({ page }) => {
    const world = createMockWorld();
    const runId = LIVE ? LIVE_ABORT_RUN : ABORT_RUN_ID;
    const destinations = watchDestinations(page);

    await injectAuth(page);
    if (!LIVE) await mockGateway(page, world);

    await page.goto(`${BASE_URL}/activity?id=${encodeURIComponent(runId)}`);

    const panel = page.getByRole('region', { name: /live run controls/i });
    await expect(panel).toBeVisible({ timeout: 15_000 });

    // Abort must not fire on the first click.
    let abortPosted = false;
    page.on('request', (request) => {
      if (request.url().includes('/agent/abort') && request.method() === 'POST') {
        abortPosted = true;
      }
    });

    await panel.getByRole('button', { name: /^abort$/i }).click();
    await expect(panel.getByTestId('abort-confirm')).toBeVisible();
    expect(abortPosted, 'abort must require explicit confirmation before sending').toBe(false);
    await expect(panel.getByTestId('abort-confirm')).toContainText(/cannot be resumed/i);

    const abortRequest = page.waitForRequest(
      (request) => request.url().includes('/agent/abort') && request.method() === 'POST',
    );
    await panel.getByRole('button', { name: /confirm abort/i }).click();
    const sentAbort = await abortRequest;
    expect(JSON.parse(sentAbort.postData() || '{}').command_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );

    // Acknowledgement, then finalisation: the run reaches a terminal outcome and
    // the controls withdraw themselves rather than offering actions on a
    // finished run.
    await expect(panel.getByTestId('command-journal')).toContainText(
      /queued|handed to the agent|applied/i,
    );
    await expect(panel.getByTestId('control-phase')).toHaveText(/finished/i, {
      timeout: 20_000,
    });
    await expect(panel.getByRole('button', { name: /^abort$/i })).toHaveCount(0);
    await expect(page.getByRole('dialog').locator('dt').filter({ hasText: /^Status$/ }).locator('..').locator('dd').getByText('Aborted', { exact: true })).toBeVisible({ timeout: 20_000 });

    expect(destinations.some((url) => url.includes(FORBIDDEN_HOST))).toBe(false);
  });

  test('offers no controls when the feature flag is off', async ({ page }) => {
    // The rollback lever. If this fails, "switch the flag off" is not a rollback.
    const world = createMockWorld();
    await injectAuth(page);
    let base = BASE_URL;
    if (LIVE) {
      base = process.env.CONTROL_E2E_DISABLED_URL || '';
      expect(base, 'Live flag-off acceptance requires a fixture serving the same bundle with controls disabled').not.toBe('');
    } else {
      await mockGateway(page, world);
      await page.route('**/api/features', (route) =>
        json(route, { features: { agent_control: false } }),
      );
    }

    await page.goto(`${base}/activity?id=${LIVE ? encodeURIComponent(LIVE_PAUSE_RUN) : RUN_ID}`);
    // The detail modal still opens; only the controls are absent.
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('region', { name: /live run controls/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^pause$/i })).toHaveCount(0);
  });
});
