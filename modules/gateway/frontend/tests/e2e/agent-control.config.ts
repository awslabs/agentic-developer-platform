/**
 * Dedicated Playwright config for the live run-control scenario — Issue #3966.
 *
 * Usage:
 *   cd modules/gateway/frontend
 *   npm install -D @playwright/test && npx playwright install chromium
 *
 *   # Mocked: real browser, real bundle, stubbed gateway. No AWS needed.
 *   npx playwright test --config tests/e2e/agent-control.config.ts
 *
 *   # Live: drives a deployed gateway. Produces Wave 4 acceptance evidence.
 *   CONTROL_E2E_LIVE=1 \
 *   GATEWAY_URL="https://<distribution>" \
 *   CONTROL_E2E_RUN_ID="<controllable run id>" \
 *   CONTROL_E2E_ABORT_RUN_ID="<second run id>" \
 *     npx playwright test --config tests/e2e/agent-control.config.ts
 *
 * ---------------------------------------------------------------------------
 * Why this config is separate, and why @playwright/test is not a dependency
 * ---------------------------------------------------------------------------
 *
 * This repo has no Playwright runner: `tests/e2e/*.spec.ts` are run by hand and
 * `@playwright/test` appears in no package.json. Two deliberate choices follow.
 *
 * 1. A dedicated config rather than a root one. Adding a root `playwright.config.ts`
 *    would create a default project that collects every existing spec in this
 *    directory — specs written for manual invocation against varying
 *    environments, which would start failing in anything that picked the config
 *    up. This config's `testMatch` names one file.
 *
 * 2. @playwright/test stays a documented on-demand install rather than a
 *    devDependency. Adding it pulls a browser-download postinstall into every
 *    `npm ci` in this package, including the gateway image build and the unit
 *    test job, neither of which run browsers. The story asks for a documented
 *    dedicated runner, which this is; it does not ask to make Playwright a
 *    build-time cost for everyone. `tsc` is likewise not pointed at this file —
 *    it would not resolve the import until someone installs it. Which is also
 *    the honest statement of status: this scenario is run deliberately, not on
 *    every commit, and a green CI badge should never be read as having run it.
 *
 * Retries are 0 on purpose. A control that only works on the second attempt is
 * a defect in the control, and retrying would hide exactly the flakiness this
 * scenario exists to detect.
 */

import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const LIVE = process.env.CONTROL_E2E_LIVE === '1';

export default defineConfig({
  testDir: fileURLToPath(new URL('.', import.meta.url)),
  testMatch: 'agent-control.spec.ts',

  // Serial: live mode drives real runs whose state the scenario mutates, so
  // parallel workers would race over the same worker process.
  fullyParallel: false,
  workers: 1,

  // See the note above: no retries, and a failure is a failure.
  retries: 0,
  forbidOnly: true,

  // Live polling settles over seconds, not milliseconds; the per-assertion
  // timeouts in the spec are the real deadlines.
  timeout: LIVE ? 180_000 : 90_000,
  expect: { timeout: 15_000 },

  reporter: [
    ['list'],
    // Written for the wave-4 evidence directory: the harness attaches this.
    ['json', { outputFile: process.env.CONTROL_E2E_REPORT || 'agent-control-report.json' }],
  ],

  use: {
    baseURL: process.env.GATEWAY_URL || 'http://localhost:5173',
    // Evidence for a control action that claims to have happened.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
