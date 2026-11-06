import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { restrictToFirstParty } from './browser_boundary.mjs';

export async function recover(input, chromium) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ serviceWorkers: 'block',
      ...(input.kind === 'missing' || input.kind === 'invalid' ? {} : { storageState: input.storage_state }) });
    try {
      await restrictToFirstParty(context, input.gateway_url);
      const page = await context.newPage();
      const scope = input.scope;
      const prefix = `/api/v1/products/${encodeURIComponent(scope.product_id)}/projects/${encodeURIComponent(scope.project_id)}/services/devops`;
      const launchPath = `${prefix}/bindings/${encodeURIComponent(input.binding_id)}/browser-launch`;
      const path = input.kind === 'allowed'
        ? `/products/${encodeURIComponent(scope.product_id)}/projects/${encodeURIComponent(scope.project_id)}/services/devops` : '/';
      await page.goto(`${input.gateway_url}${path}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      if (new URL(page.url()).origin !== new URL(input.gateway_url).origin) throw new Error('not first-party');
      let status;
      if (input.kind === 'allowed') {
        const operations = page.getByRole('region', { name: 'DevOps operations' });
        await operations.getByRole('combobox', { name: 'Existing DevOps bindings' }).selectOption(input.binding_id);
        const responsePromise = page.waitForResponse(response => response.request().method() === 'POST'
          && new URL(response.url()).pathname === launchPath, { timeout: 30000 });
        await operations.getByRole('button', { name: 'Open native DevOps UI' }).click();
        const response = await responsePromise;
        status = response.status();
        if (status === 200) {
          const handoff = await response.json();
          if (handoff.binding_id !== input.binding_id || new URL(handoff.launch_url).hostname !== input.provider_hostname
            || new URL(handoff.launch_url).protocol !== 'https:') throw new Error('unexpected handoff');
        }
      } else {
        status = await page.evaluate(async ({ path, revision, token }) => {
          const headers = { 'Content-Type': 'application/json' };
          if (token !== null) headers.Authorization = `Bearer ${token}`;
          const response = await fetch(path, { method: 'POST', headers, redirect: 'manual',
            body: JSON.stringify({ configuration_revision: revision }) });
          return response.status;
        }, { path: launchPath, revision: input.configuration_revision, token: input.token });
      }
      return { status };
    } finally {
      await context.close();
    }
  } finally {
    await browser.close();
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  try {
    const input = JSON.parse(readFileSync(0, 'utf8'));
    const require = createRequire(`${process.cwd()}/package.json`);
    const { chromium } = require('@playwright/test');
    process.stdout.write(JSON.stringify(await recover(input, chromium)));
  } catch {
    process.exitCode = 1;
  }
}
