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

import {
  ScenarioRecorder,
  installVisibilityControl,
  setDocumentHidden,
  intervalsFrom,
  summarizeWindow,
  redactUrl,
  VISIBILITY_MECHANISM,
  SENTINEL_POD_ADDRESS,
  SENTINEL_POD_TOKEN,
  MOCK_RUN_ID,
  MOCK_ABORT_RUN_ID,
  CaptureInputError,
} from './agent-control-capture';

// Generated from src.activity.schemas.InvocationItem, including nullable defaults.
const invocationTemplate = JSON.parse(readFileSync(new URL('./fixtures/agent-control-invocation.json', import.meta.url), 'utf8'));

const BASE_URL = process.env.GATEWAY_URL || 'http://localhost:5173';
const LIVE = process.env.CONTROL_E2E_LIVE === '1';

/** Runs supplied for live mode. Unused in mocked mode. */
const LIVE_PAUSE_RUN = process.env.CONTROL_E2E_RUN_ID || '';
const LIVE_ABORT_RUN = process.env.CONTROL_E2E_ABORT_RUN_ID || '';

/**
 * Pod coordinates that must never appear in browser traffic.
 *
 * Shared with the capture module so the leak measurement and the assertions here
 * are looking for the same values — two independent copies could drift into a
 * state where the test watches for one string and the evidence reports on another.
 */
const FORBIDDEN_HOST = SENTINEL_POD_ADDRESS;
const FORBIDDEN_TOKEN = SENTINEL_POD_TOKEN;

/**
 * The capture directory, required for every run.
 *
 * Global setup has already validated it; this is the per-scenario read. It throws
 * rather than defaulting, because a scenario that quietly wrote its fragment
 * somewhere else would leave the merge looking like a scenario that never ran.
 */
function requireCaptureDir(): string {
  const dir = (process.env.CONTROL_E2E_CAPTURE_DIR || '').trim();
  if (!dir) {
    throw new CaptureInputError(
      'CONTROL_E2E_CAPTURE_DIR is unset inside a scenario; global setup should have refused this run',
    );
  }
  return dir;
}

/**
 * Real session material this run must never archive.
 *
 * Passed to the leak watch so that a live capture is checked against the ACTUAL
 * tokens in play, not only against a sentinel. A producer that redacts its
 * sentinel while writing a real access token has redacted the wrong thing.
 */
function sessionSecrets(): string[] {
  const secrets: string[] = [];
  for (const variable of ['CONTROL_E2E_SESSION_FILE', 'CONTROL_E2E_NONOWNER_SESSION_FILE']) {
    const path = (process.env[variable] || '').trim();
    if (!path) continue;
    try {
      const session = JSON.parse(readFileSync(path, 'utf8'));
      for (const field of ['access_token', 'id_token', 'refresh_token']) {
        if (typeof session[field] === 'string' && session[field]) secrets.push(session[field]);
      }
    } catch {
      // Setup validates these files; an unreadable one here is not this helper's
      // error to report.
    }
  }
  return secrets;
}

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

/**
 * The control verbs the panel is currently OFFERING, read from the DOM.
 *
 * Derived from the rendered buttons rather than from the capability response, so
 * that the evaluator's "offered minus allowed must be empty" subtraction compares
 * two independently observed sets. The names are normalised to the verbs the
 * gateway uses, because the button labels are operator-facing copy that may be
 * reworded without the capability names changing.
 */
async function renderedControls(panel: ReturnType<Page['getByRole']>): Promise<string[]> {
  const labels = await panel.getByRole('button').allInnerTexts();
  const verbs = new Set<string>();
  for (const label of labels) {
    const text = label.trim().toLowerCase();
    // "Confirm abort" is the second step of abort, not a fifth verb.
    if (/^pause\b/.test(text)) verbs.add('pause');
    else if (/^resume\b/.test(text)) verbs.add('resume');
    else if (/abort/.test(text)) verbs.add('abort');
    else if (/instruction|steer/.test(text)) verbs.add('steer');
  }
  return [...verbs].sort();
}

/**
 * Ask the gateway to accept a command as somebody who does not own the run.
 *
 * The measurement is the STATUS THE SERVER RETURNED, taken from the response the
 * browser received. That distinction is the whole point: a refusal proven by
 * `page.route` fulfilling a 403 proves only that this test can write a 403, so in
 * live mode no route is installed and the answer comes from the deployment. In
 * mocked mode the refusal is manufactured, and it is recorded as an injected
 * condition so the capture says so — and `verifyForLiveAcceptance` then refuses
 * the artifact for live use on exactly that basis.
 *
 * Run through the page's own fetch rather than an out-of-band HTTP client so the
 * request carries the browser's real credential handling, and so the leak watch
 * sees it like any other request.
 */
async function probeNonowner(
  page: Page,
  recorder: ScenarioRecorder,
  runId: string,
): Promise<boolean> {
  const path = `/api/activity/invocations/${encodeURIComponent(runId)}/agent/pause`;
  let nonownerToken = '';

  if (!LIVE) {
    // One-shot: installed last so it takes precedence, and removed immediately
    // after so the rest of the scenario still talks to the mocked world.
    await page.route(path, (route) => json(route, { detail: 'not your run' }, 403));
    recorder.inject(`command_response:http_403_for_nonowner (${path})`);
  } else {
    // The second identity must be a real session; setup has already required the
    // file. A fabricated token is never injected in live mode.
    const file = process.env.CONTROL_E2E_NONOWNER_SESSION_FILE as string;
    const session = JSON.parse(readFileSync(file, 'utf8'));
    if (typeof session.access_token !== 'string' || !session.access_token ||
        typeof session.expires_at_ms !== 'number' || session.expires_at_ms <= Date.now()) {
      throw new Error('Non-owner fixture session is missing a token or has expired');
    }
    nonownerToken = session.access_token;
  }

  const status = await page.evaluate(async ({ target, token }) => {
    try {
      const response = await fetch(target, {
        method: 'POST',
        headers: { 'content-type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ command_id: crypto.randomUUID() }),
      });
      return response.status;
    } catch {
      // A transport failure is not a refusal. Reported as 0 so it cannot be read
      // as the gateway having decided anything.
      return 0;
    }
  }, { target: path, token: nonownerToken });

  if (!LIVE) await page.unroute(path);

  recorder.measured.nonowner_probe = { url: path, status };
  // 403 and 404 both mean refused — the gateway deliberately fuses "not found"
  // with "not yours" so a caller learns nothing from the difference. 0 is not a
  // refusal, and neither is any 2xx.
  return status === 403 || status === 404;
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

// Imported rather than redeclared: the capture records these as the run ids it
// describes, so the scenarios must drive the very same ones.
const RUN_ID = MOCK_RUN_ID;
const ABORT_RUN_ID = MOCK_ABORT_RUN_ID;

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
      // null, not a number: a worker that does not report a tool count is the
      // case AC-P4 cares about, because that is where a dashboard is tempted to
      // render an unproven zero as quiescence. A mock that always supplies a
      // count would never exercise the honest-wording path.
      activeTools: null,
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

    const recorder = new ScenarioRecorder('control-lifecycle', sessionSecrets());
    recorder.attach(page);

    await injectAuth(page);
    if (!LIVE) await mockGateway(page, world);

    // The existing deep link, not a new route.
    await page.goto(`${BASE_URL}/activity?id=${encodeURIComponent(runId)}`);

    const panel = page.getByRole('region', { name: /live run controls/i });
    await expect(panel).toBeVisible({ timeout: 15_000 });

    // The starting phase, read from the DOM before anything is clicked.
    await expect(panel.getByTestId('control-phase')).toHaveText(/running/i, { timeout: 15_000 });
    recorder.recordPhase('running');

    // Sample from here on, so the transient phases between the click and the
    // settled state are observed rather than inferred.
    const stopWatching = recorder.watchPhases(page);

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
    // Record at the moment the assertion confirmed it. The background sampler may
    // also catch this, but an assertion that waited for a phase IS an observation
    // of it, and relying on the sampler alone leaves the sequence at the mercy of
    // where its 120ms tick happened to fall. recordPhase de-duplicates, so the two
    // sources cannot double-count.
    recorder.recordPhase('pause requested');
    await expect(panel.getByText(/spend may continue/i)).toBeVisible();

    // Then the tool-side-effect evidence: the run actually settles to paused.
    await expect(panel.getByTestId('control-phase')).toHaveText(/^Paused$/, {
      timeout: 15_000,
    });
    recorder.recordPhase('paused');

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
    recorder.recordPhase('running');

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

    // ---- measurement -----------------------------------------------------
    stopWatching();

    // The phases the DOM actually displayed, in the order it displayed them.
    // Built by the sampler above, so `pause_requested` is present because it was
    // seen — not because the mock's transition table says it should have been.
    recorder.measured.phase_sequence = recorder.phaseSequence;

    // Honest wording, read from the rendered DOM. Two separate observations
    // because they are two separate claims the operator relies on: that pausing
    // does not stop the meter, and that an unknown tool count is reported as
    // unknown rather than silently as zero.
    recorder.measured.pause_copy_mentions_spend = recorder.pauseRequestedCopy.some((copy) =>
      /spend may continue/i.test(copy),
    );
    recorder.measured.active_tool_reason = (
      await panel.getByTestId('active-tools').first().innerText()
    ).trim();

    // What the gateway advertised versus what the UI drew. Independent sources —
    // see the note on the capabilities listener in the recorder.
    recorder.measured.advertised_capabilities = recorder.advertisedCapabilities;
    recorder.measured.rendered_controls = await renderedControls(panel);

    // The steer command as it went out, with the instruction body deliberately
    // absent: an instruction is user content, and this artifact is kept as
    // evidence. What matters for the check is that the request happened, where it
    // went and that it carried an idempotency key.
    recorder.measured.steer_request = {
      method: sentSteer.method(),
      url: redactUrl(sentSteer.url()),
      has_command_id: typeof JSON.parse(sentSteer.postData() || '{}').command_id === 'string',
      instruction_length: String(JSON.parse(sentSteer.postData() || '{}').instruction ?? '').length,
    };
    recorder.measured.steer_status_sequence = recorder.commandStatuses;

    // State polling is not a detail refresh: observe the invocation detail route.
    const lastCommandAt = recorder.commandRequests().reduce((latest, request) => Math.max(latest, request.at_ms), 0);
    recorder.measured.detail_refreshed_after_command =
      recorder.requests.some((request) => request.at_ms > lastCommandAt && request.method === 'GET' &&
        request.url.endsWith(`/api/me/agent-invocations/${encodeURIComponent(runId)}`));
    expect(recorder.measured.detail_refreshed_after_command).toBe(true);

    await recorder.finish(requireCaptureDir());
  });

  test('abort a second run and observe its terminal evidence', async ({ page }) => {
    const world = createMockWorld();
    const runId = LIVE ? LIVE_ABORT_RUN : ABORT_RUN_ID;
    const destinations = watchDestinations(page);

    // The authorization scenario rides on this run, because a terminal run is the
    // state in which "submit is blocked" is a meaningful measurement, and aborting
    // is how the browser reaches one by observation rather than by assumption.
    const recorder = new ScenarioRecorder('authorization', sessionSecrets());
    recorder.attach(page);

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

    // ---- measurement -----------------------------------------------------
    // The run is now terminal by observation: the phase reads finished and the
    // status reads Aborted, both checked above. So the absence of a submit here is
    // "blocked on a run that really ended", not "blocked before anything started".
    //
    // This is measured and asserted BEFORE the non-owner probe below, because that
    // probe deliberately submits a command out of band. Mixing the two would let a
    // submission this test made itself count against the UI, and an earlier
    // revision of this file did exactly that — the assertion caught it.
    const terminalBoundary = recorder.now();
    const commandsBeforeProbe = recorder.commandRequests().length;
    await page.waitForTimeout(1_000);
    recorder.measured.terminal_submit_blocked =
      (await renderedControls(panel)).length === 0 &&
      recorder.commandRequests(terminalBoundary).length === 0;
    expect(
      recorder.commandRequests().length,
      'the dashboard must submit no command once the run has reached a terminal state',
    ).toBe(commandsBeforeProbe);

    // A non-owner's attempt. In mocked mode the gateway is made to answer 403 for
    // the second identity — recorded as an injected condition, because a mocked
    // refusal is not the deployment refusing. In live mode the real second session
    // drives it and the refusal is the gateway's own.
    const nonownerBlocked = await probeNonowner(page, recorder, runId);
    recorder.measured.nonowner_submit_blocked = nonownerBlocked;

    // Where the browser actually sent things, redacted to origin + path. This is
    // the set W4-06 will read; it is populated here because an artifact missing it
    // is rejected wholesale even though no predicate consumes it yet.
    recorder.measured.request_destinations = recorder.destinations.map(redactUrl);

    // Leak booleans from in-memory body comparison. The sentinel pod address and
    // token were served to this page in every state response (see mockGateway), so
    // a `false` here is a negative result from a real opportunity to leak, not the
    // absence of a test.
    recorder.measured.request_bodies_contain_pod_address = recorder.leaks.podAddress;
    recorder.measured.request_bodies_contain_token = recorder.leaks.token;
    recorder.measured.spoofed_identity_rejected = nonownerBlocked;

    await recorder.finish(requireCaptureDir());
  });

  test('offers no controls when the flag is off, loading or erroring', async ({ page }) => {
    // The rollback lever, plus the two cases that catch a real fail-open bug.
    // "Flag off" is the easy one. The dangerous ones are "still loading" and
    // "backend error", where an implementation that treats an absent answer as
    // permission renders controls that may not be permitted.
    //
    // Each of the three is MEASURED as a pair of counts — control nodes found and
    // command requests issued — because neither implies the other. Zero nodes
    // with a command request means an invisible control still acted; zero
    // requests with rendered nodes means the operator was offered a button that
    // silently did nothing.
    const recorder = new ScenarioRecorder('flag-gating', sessionSecrets());
    recorder.attach(page);
    await installVisibilityControl(page);
    await injectAuth(page);

    const runId = LIVE ? LIVE_PAUSE_RUN : RUN_ID;

    /** Count what one gated render produced. */
    const measureRender = async (base: string, label: string) => {
      const before = recorder.commandRequests().length;
      await page.goto(`${base}/activity?id=${encodeURIComponent(runId)}`);
      // The detail modal still opens; only the controls are absent.
      await expect(page.getByRole('dialog')).toBeVisible({ timeout: 15_000 });
      // Give a fail-open implementation time to render something before counting.
      await page.waitForTimeout(1_500);
      const controlNodes =
        (await page.getByRole('region', { name: /live run controls/i }).count()) +
        (await page.getByRole('button', { name: /^(pause|resume|abort)$/i }).count());
      const commandRequests = recorder.commandRequests().length - before;
      return { control_nodes: controlNodes, command_requests: commandRequests, label };
    };

    // ---- flag off --------------------------------------------------------
    let offBase = BASE_URL;
    if (LIVE) {
      // A real deployment serving the same bundle with the flag off. Validated in
      // global setup, so this is a second read rather than the first check.
      offBase = process.env.CONTROL_E2E_DISABLED_URL || '';
      expect(
        offBase,
        'Live flag-off acceptance requires a fixture serving the same bundle with controls disabled',
      ).not.toBe('');
    } else {
      await mockGateway(page, createMockWorld());
      await page.route('**/api/features', (route) =>
        json(route, { features: { agent_control: false } }),
      );
    }
    recorder.measured.flag_off = await measureRender(offBase, 'off');

    // ---- flag still loading ---------------------------------------------
    // Injected, and recorded as injected: an evidence file that cannot tell "the
    // deployment was broken" from "we held its response open for ten seconds" is
    // misleading in the more alarming direction.
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    if (!LIVE) await mockGateway(page, createMockWorld());
    recorder.inject('features_endpoint:held_open (flag_loading render)');
    await page.route('**/api/features', async () => {
      // Never fulfilled: the flag query stays pending for the life of the page.
    });
    recorder.measured.flag_loading = await measureRender(BASE_URL, 'still loading');

    // ---- flag errored ----------------------------------------------------
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    if (!LIVE) await mockGateway(page, createMockWorld());
    recorder.inject('features_endpoint:http_500 (flag_error render)');
    await page.route('**/api/features', (route) => json(route, { detail: 'injected failure' }, 500));
    recorder.measured.flag_error = await measureRender(BASE_URL, 'erroring');

    // All three must be zero on both counts; asserted here as well as in the
    // evaluator so a fail-open bundle fails the run that produced the evidence.
    for (const key of ['flag_off', 'flag_loading', 'flag_error'] as const) {
      const observation = recorder.measured[key] as { control_nodes: number; command_requests: number };
      expect(observation.control_nodes, `${key}: controls must not render`).toBe(0);
      expect(observation.command_requests, `${key}: no command may be sent`).toBe(0);
    }

    await recorder.finish(requireCaptureDir());
  });

  test('polls on the documented interval and stops when nobody is watching', async ({ page }) => {
    // The cost scenario. Every measurement here is a TIMED OBSERVATION, never a
    // constant read from the component: `CONTROL_POLL_MS` says what the code
    // intends, and the failures that matter — a poll that never stops, a retry
    // that never backs off — are precisely the ones a source read cannot see.
    //
    // The three "stopped" claims are each tied to a window with recorded bounds
    // and sampled visibility, so a `false` cannot be produced without time having
    // passed. A missing or zero-length window is incomplete, not false.
    const world = createMockWorld();
    const runId = LIVE ? LIVE_PAUSE_RUN : RUN_ID;
    const recorder = new ScenarioRecorder('polling-lifecycle', sessionSecrets());
    recorder.attach(page);

    // Browser-level visibility, installed before any navigation. See
    // VISIBILITY_MECHANISM for the three alternatives that do not work in
    // headless Chromium and why this one does.
    await installVisibilityControl(page);
    await injectAuth(page);
    if (!LIVE) await mockGateway(page, world);

    await page.goto(`${BASE_URL}/activity?id=${encodeURIComponent(runId)}`);
    const panel = page.getByRole('region', { name: /live run controls/i });
    await expect(panel).toBeVisible({ timeout: 15_000 });

    // ---- the steady-state interval ---------------------------------------
    // Watch for long enough to see several polls, then derive the intervals from
    // the gaps between the requests that actually arrived.
    const visibleFrom = recorder.now();
    await page.waitForTimeout(7_000);
    const steady = recorder.stateRequests(visibleFrom);
    expect(
      steady.length,
      'the panel must poll more than once while visible, or there is no interval to measure',
    ).toBeGreaterThan(2);
    recorder.measured.poll_intervals_ms = intervalsFrom(steady);

    // ---- hidden document -------------------------------------------------
    await setDocumentHidden(page, true);
    const hidden = await recorder.observeWindow(page, 'polled_while_hidden', VISIBILITY_MECHANISM, 6_000);
    // summarizeWindow throws on a zero-duration window: an interval that did not
    // elapse cannot support a claim that nothing happened during it.
    recorder.measured.polled_while_hidden = summarizeWindow(hidden).polled;
    expect(
      hidden.observed_visibility.every((state) => state === 'hidden'),
      'the window must actually have been hidden for the whole time it was observed',
    ).toBe(true);
    await setDocumentHidden(page, false);

    // ---- closed modal ----------------------------------------------------
    // Visible again, so this window isolates the modal being shut from the tab
    // being backgrounded — two separate reasons to stop, measured separately.
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 10_000 });
    const closed = await recorder.observeWindow(page, 'polled_after_close', 'modal dismissed via Escape', 6_000);
    recorder.measured.polled_after_close = summarizeWindow(closed).polled;
    expect(
      closed.observed_visibility.every((state) => state === 'visible'),
      'the closed-modal window must be measured with the document visible, or it proves the wrong thing',
    ).toBe(true);

    // ---- terminal run ----------------------------------------------------
    // Reopen on a run the world has already finished. Polling must stop because
    // there is nothing left to learn, not because the tab is hidden.
    if (!LIVE) {
      world.runs[runId].state = 'terminal';
      world.runs[runId].status = 'aborted';
      world.runs[runId].capabilities = { pause: false, resume: false, steer: false, abort: false };
    }
    // The preceding authorization scenario actually aborted this second live run.
    const terminalRunId = LIVE ? LIVE_ABORT_RUN : runId;
    await page.goto(`${BASE_URL}/activity?id=${encodeURIComponent(terminalRunId)}`);
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('control-phase')).toHaveText(/finished/i);
    // Let the first read land and be recognised as terminal before opening the
    // window, so the initial poll is not counted as continued polling.
    await page.waitForTimeout(2_500);
    const terminal = await recorder.observeWindow(page, 'polled_after_terminal', 'run state terminal', 6_000);
    recorder.measured.polled_after_terminal = summarizeWindow(terminal).polled;

    // ---- backoff ---------------------------------------------------------
    // A failing endpoint must be retried more slowly each time. Injected, and
    // recorded as injected: this is a deliberate fault, not a deployment defect.
    if (!LIVE) {
      world.runs[runId].state = 'running';
      world.runs[runId].status = 'in_progress';
      world.runs[runId].capabilities = { pause: true, resume: true, steer: true, abort: true };
    }
    recorder.inject('state_endpoint:http_503_sustained (backoff measurement)');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    if (!LIVE) await mockGateway(page, world);
    await page.route('**/api/activity/invocations/*/agent/state', (route) =>
      json(route, { detail: 'injected failure' }, 503),
    );

    const failingFrom = recorder.now();
    await page.goto(`${BASE_URL}/activity?id=${encodeURIComponent(runId)}`);
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 15_000 });
    // Long enough for several retries at a widening spacing.
    await page.waitForTimeout(25_000);
    const failing = recorder.stateRequests(failingFrom);
    expect(
      failing.length,
      'the panel must retry a failing state endpoint, or there is no backoff to measure',
    ).toBeGreaterThan(2);
    const backoff = intervalsFrom(failing);
    recorder.measured.backoff_intervals_ms = backoff;

    // Backoff widens until the documented 30-second cap.
    expect(
      backoff.every((interval, index) => index === 0 || interval > backoff[index - 1] || (interval === backoff[index - 1] && interval >= 30000)),
      `observed error intervals ${JSON.stringify(backoff)} must strictly widen: retrying a failing ` +
        'control endpoint at a constant rate turns one backend problem into a load problem',
    ).toBe(true);

    await recorder.finish(requireCaptureDir());
  });
});
