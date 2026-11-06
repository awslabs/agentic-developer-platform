import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(`${process.cwd()}/package.json`);
const { chromium } = require('@playwright/test');
const browser = await chromium.launch({ headless: true });
if (process.argv.includes('--check-dependencies')) {
  await browser.close();
  process.stdout.write(JSON.stringify({ browser: 'chromium', ready: true }));
  process.exit(0);
}
try {
  const input = JSON.parse(readFileSync(0, 'utf8'));
  const context = await browser.newContext({ storageState: input.storage_state });
  const page = await context.newPage();
  const { product_id, project_id, tenant_id } = input.scope;
  const prefix = `/api/v1/products/${encodeURIComponent(product_id)}/projects/${encodeURIComponent(project_id)}/services/finops`;
  const url = `${input.frontend_url}/products/${encodeURIComponent(product_id)}/projects/${encodeURIComponent(project_id)}/services/finops`;
  await page.route('**/*', async route => {
    if (route.request().isNavigationRequest() && new URL(route.request().url()).protocol !== 'https:') {
      await route.abort();
    } else {
      await route.continue();
    }
  });
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  if (new URL(page.url()).origin !== new URL(input.frontend_url).origin
    || new URL(page.url()).pathname !== `/products/${encodeURIComponent(product_id)}/projects/${encodeURIComponent(project_id)}/services/finops`) {
    throw new Error('first-party UI navigation left the selected Product');
  }
  const resultButton = page.getByRole('button', { name: new RegExp(`^${input.result_id}: `) });
  const requestPath = `${prefix}/results/${encodeURIComponent(input.result_id)}`;
  const responsePromise = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === requestPath, { timeout: 30000 });
  await resultButton.click();
  const response = await responsePromise;
  if (response.status() !== 200) throw new Error('scoped UI readback unavailable');
  const observed = await response.json();
  if (observed.result_id !== input.result_id || observed.revision !== input.revision
    || observed.scope?.tenant_id !== tenant_id || observed.scope?.product_id !== product_id
    || observed.scope?.project_id !== project_id
    || JSON.stringify(observed.native?.answer_artifact) !== JSON.stringify(input.native_artifact)
    || JSON.stringify(observed.oracle?.pages) !== JSON.stringify(input.billing_pages)) {
    throw new Error('UI selected another native or billing result');
  }
  const display = page.getByLabel('FinOps answer and evidence');
  await display.getByRole('heading', { name: 'Original native answer' }).waitFor();
  const nativeText = await display.locator('pre').nth(1).innerText();
  const native = JSON.parse(await display.locator('pre').nth(2).innerText());
  const billing = JSON.parse(await display.locator('pre').nth(3).innerText());
  if (nativeText !== input.answer_text) throw new Error('UI did not display the original native answer content');
  if (JSON.stringify(native.answer_artifact) !== JSON.stringify(input.native_artifact)
    || JSON.stringify(billing.pages) !== JSON.stringify(input.billing_pages)) {
    throw new Error('UI did not display original native and independent billing evidence');
  }
  process.stdout.write(JSON.stringify({ result_id: observed.result_id, revision: observed.revision,
    native_artifact: native.answer_artifact, billing_pages: billing.pages, answer_text: nativeText }));
  await context.close();
} finally {
  await browser.close();
}
