/** Local HTTP SSE fixture: browser acceptance, not cloud acceptance. */
import { test, expect } from '@playwright/test';
import { createServer, type ServerResponse } from 'node:http';
import { readFileSync } from 'node:fs';
const template = JSON.parse(readFileSync(new URL('./fixtures/agent-control-invocation.json', import.meta.url), 'utf8'));
for (const mobile of [false, true]) test(`delivery before completion (${mobile ? 'mobile' : 'desktop'})`, async ({ page }, testInfo) => {
  page.on('pageerror', error => console.error(error));
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  let feed: ServerResponse | undefined;
  const server = createServer((req, res) => {
    expect(req.headers.authorization).toMatch(/^Bearer /);
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*' });
    res.flushHeaders(); feed = res;
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as { port: number }).port;
  try {
    await page.addInitScript(() => {
      const expiry = Date.now() + 3600000;
      const token = `${btoa('{}')}.${btoa(JSON.stringify({ sub: 'owner', email: 'owner@example.com', 'custom:role': 'member', 'custom:org_id': 'tenant', exp: Math.floor(expiry / 1000), auth_time: Math.floor(Date.now() / 1000) }))}.test`;
      sessionStorage.setItem('cognito_access_token', token); sessionStorage.setItem('cognito_id_token', token);
      sessionStorage.setItem('cognito_token_expiry', String(expiry));
    });
    const invocation = { ...template, invocation_id: 'stream-run', status: 'in_progress', persona: 'developer', liveness: 'live', summary: 'Streaming demonstration' };
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/agent/events')) return route.continue({ url: `http://127.0.0.1:${port}/events` });
      let body: unknown = {};
      if (path.endsWith('/features')) body = { features: { agent_explanations: true, agent_control: false } };
      else if (path.endsWith('/access/status')) body = { status: 'registered' };
      else if (path.endsWith('/auth/workspaces')) body = { items: [] };
      else if (path.endsWith('/agent-invocations/stream-run')) body = invocation;
      else if (path.includes('/agent-invocations')) body = { items: [invocation], last_key: null };
      return route.fulfill({ json: body });
    });
    await page.goto('/activity?id=stream-run');
    await expect(page.getByRole('heading', { name: 'Implementation explanations' })).toBeVisible();
    await expect.poll(() => !!feed).toBe(true);
    for (let sequence = 1; sequence <= 2; sequence++) {
      const text = sequence === 1 ? 'Mechanism: bounded history lets reconnects replay updates.' : 'Evidence: updates arrive before completion. Cloud delivery remains untested.';
      const start = Date.now();
      feed!.write(`id: stream-run:1:${sequence}\nevent: explanation\ndata: ${JSON.stringify({ version: 1, invocation_id: 'stream-run', generation: 1, sequence, timestamp: new Date().toISOString(), kind: 'explanation', payload: { text } })}\n\n`);
      await expect(page.getByText(text, { exact: true })).toBeVisible({ timeout: 5000 });
      expect(Date.now() - start).toBeLessThan(5000); expect(feed!.writableEnded).toBe(false);
    }
    await page.screenshot({ path: testInfo.outputPath('live-explanations.png') });
    await expect(page.getByRole('button', { name: /^Pause$/ })).toHaveCount(0);
    await page.getByRole('button', { name: /Jump to latest/ }).focus();
    await expect(page.getByRole('button', { name: /Jump to latest/ })).toBeFocused();
    await page.keyboard.press('Enter'); await page.keyboard.press('Escape');
    await expect.poll(() => feed!.destroyed).toBe(true);
  } finally {
    feed?.destroy(); server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});
