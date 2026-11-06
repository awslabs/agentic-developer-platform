import { appendFileSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { restrictToFirstParty } from './browser_boundary.mjs';

const input = JSON.parse(readFileSync(0, 'utf8'));
const require = createRequire(`${process.cwd()}/package.json`);
const { chromium } = require('@playwright/test');
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ storageState: input.storage_state, serviceWorkers: 'block' });
  await restrictToFirstParty(context, input.url);
  const page = await context.newPage();
  const scope = input.scope;
  const prefix = `/api/v1/products/${scope.product_id}/projects/${scope.project_id}/services/devops`;
  await page.goto(`${input.url}/products/${encodeURIComponent(scope.product_id)}/projects/${encodeURIComponent(scope.project_id)}/services/devops`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  if (new URL(page.url()).origin !== new URL(input.url).origin) throw new Error('not first-party');
  const operations = page.getByRole('region', { name: 'DevOps operations' });
  await operations.getByRole('combobox', { name: 'Existing DevOps bindings' }).selectOption(scope.binding_id);
  await operations.getByLabel('Typed run input (JSON)').fill(JSON.stringify(input.input.devops_input));
  let receipt;
  await page.route('**/api/v1/products/**/runs', async route => {
    const request = route.request();
    if (request.method() === 'POST') {
      const url = new URL(request.url());
      if (url.origin !== new URL(input.url).origin) throw new Error('not first-party');
      const path = url.pathname;
      if (path !== `${prefix}/runs`) throw new Error('first-party UI selected another project');
      const body = request.postDataJSON();
      if (body.service_id !== 'devops' || body.binding_id !== scope.binding_id
        || body.configuration_revision !== input.input.configuration_revision
        || body.capability !== input.input.capability || JSON.stringify(body.devops_input) !== JSON.stringify(input.input.devops_input)) {
        throw new Error('first-party UI changed the authorized native input');
      }
      receipt = request.headers()['idempotency-key'];
      if (!receipt || !/^[A-Za-z0-9_-]{16,128}$/.test(receipt)) throw new Error('first-party UI run lacks reconciliation receipt');
      appendFileSync(input.journal, `${JSON.stringify({ entry_point: 'ui', status: 'browser_submitting', receipt })}\n`, { mode: 0o600 });
    }
    await route.fallback();
  });
  const responsePromise = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `${prefix}/runs`, { timeout: 30000 });
  await operations.getByRole('button', { name: 'Submit DevOps run' }).click();
  const response = await responsePromise;
  if (response.status() !== 202) throw new Error('first-party UI run was not admitted');
  const run = await response.json();
  if (!run.run_id || run.scope?.product_id !== scope.product_id || run.scope?.project_id !== scope.project_id) {
    throw new Error('first-party UI run is outside selected scope');
  }
  const launchPromise = page.waitForResponse(candidate => candidate.request().method() === 'POST'
    && new URL(candidate.url()).pathname === `${prefix}/bindings/${scope.binding_id}/browser-launch`, { timeout: 30000 });
  await operations.getByRole('button', { name: 'Open native DevOps UI' }).click();
  const launch = await launchPromise;
  if (launch.status() !== 200) throw new Error('first-party browser launch was denied');
  const handoff = await launch.json();
  if (handoff.binding_id !== scope.binding_id || new URL(handoff.launch_url).protocol !== 'https:'
    || new URL(handoff.launch_url).hostname !== input.provider_hostname) {
    throw new Error('native browser handoff did not match the authorized binding');
  }
  process.stdout.write(JSON.stringify({ run_id: run.run_id, receipt, native_handoff: true }));
  await context.close();
} finally {
  await browser.close();
}
