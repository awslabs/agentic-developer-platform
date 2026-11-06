import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { recover } from './browser_recovery.mjs';
import { restrictToFirstParty } from './browser_boundary.mjs';

const selection = {
  gateway_url: 'https://gateway.example.org', provider_hostname: 'signin.aws.amazon.com',
  scope: { product_id: 'product-one', project_id: 'project-one' }, binding_id: 'binding-one',
  configuration_revision: 3, storage_state: '/private/user-one.json', token: null,
};

function simulatedBrowser({ status = 200, handoff = {}, pageUrl = selection.gateway_url, fetchStatus } = {}) {
  const events = { contexts: [], closed: [], calls: [], route: null };
  const response = {
    url: () => `${selection.gateway_url}/api/v1/products/product-one/projects/project-one/services/devops/bindings/binding-one/browser-launch`,
    request: () => ({ method: () => 'POST', url: () => `${selection.gateway_url}/api/v1/products/product-one/projects/project-one/services/devops/bindings/binding-one/browser-launch` }),
    status: () => status,
    json: async () => ({ binding_id: selection.binding_id,
      launch_url: 'https://signin.aws.amazon.com/federation?SigninToken=private-ticket', ...handoff }),
  };
  const page = {
    route: async (_pattern, handler) => { events.route = handler; },
    goto: async (url) => { events.calls.push(['goto', url]); },
    url: () => pageUrl,
    getByRole: () => ({
      getByRole: (_role, options) => ({
        selectOption: async value => { events.calls.push(['select', value]); },
        click: async () => { events.calls.push(['click', options.name]); },
      }),
    }),
    waitForResponse: async predicate => {
      assert.equal(predicate(response), true);
      return response;
    },
    waitForURL: async () => { assert.fail('AWS browser navigation is prohibited'); },
    evaluate: async (callback, arguments_) => {
      events.calls.push(['fetch', arguments_]);
      const priorFetch = globalThis.fetch;
      globalThis.fetch = async (path, options) => {
        events.calls.push(['request', { path, ...options }]);
        return { status: fetchStatus ? fetchStatus(options) : status };
      };
      try {
        return await callback(arguments_);
      } finally {
        globalThis.fetch = priorFetch;
      }
    },
  };
  const chromium = { launch: async () => ({
    newContext: async options => {
      events.contexts.push(options);
      return { route: async (_pattern, handler) => { events.route = handler; },
        newPage: async () => page, close: async () => { events.closed.push('context'); } };
    },
    close: async () => { events.closed.push('browser'); },
  }) };
  return { chromium, events };
}

test('missing and invalid sessions have no stored login and make same-origin requests', async () => {
  for (const kind of ['missing', 'invalid']) {
    const { chromium, events } = simulatedBrowser({ status: 401 });
    assert.deepEqual(await recover({ ...selection, kind, token: kind === 'invalid' ? 'invalid' : null }, chromium), { status: 401 });
    assert.deepEqual(events.contexts, [{ serviceWorkers: 'block' }]);
    assert.deepEqual(events.calls[0], ['goto', `${selection.gateway_url}/`]);
    assert.deepEqual(events.calls[1], ['fetch', {
      path: '/api/v1/products/product-one/projects/project-one/services/devops/bindings/binding-one/browser-launch',
      revision: 3, token: kind === 'invalid' ? 'invalid' : null,
    }]);
    assert.deepEqual(events.calls[2], ['request', {
      path: '/api/v1/products/product-one/projects/project-one/services/devops/bindings/binding-one/browser-launch',
      method: 'POST', headers: { 'Content-Type': 'application/json',
        ...(kind === 'invalid' ? { Authorization: 'Bearer invalid' } : {}) },
      redirect: 'manual', body: '{"configuration_revision":3}',
    }]);
    assert.deepEqual(events.closed, ['context', 'browser']);
  }
});

test('allowed UI launch returns status only, not the private provider ticket', async () => {
  const { chromium, events } = simulatedBrowser();
  assert.deepEqual(await recover({ ...selection, kind: 'allowed' }, chromium), { status: 200 });
  assert.deepEqual(events.contexts, [{ storageState: selection.storage_state, serviceWorkers: 'block' }]);
  assert.deepEqual(events.calls.slice(1), [['select', 'binding-one'], ['click', 'Open native DevOps UI']]);
  assert.deepEqual(events.closed, ['context', 'browser']);
});

test('authenticated denials present the original bearer token, including after logout', async () => {
  for (const [kind, status] of [['foreign', 404], ['throttled', 429], ['revoked', 403], ['logged_out', 401]]) {
    const token = 'selected-user-access-token';
    const { chromium, events } = simulatedBrowser({ fetchStatus: options => {
      assert.equal(options.headers.Authorization, `Bearer ${token}`);
      assert.equal(options.redirect, 'manual');
      return options.headers.Authorization === `Bearer ${token}` ? status : 401;
    } });
    assert.deepEqual(await recover({ ...selection, kind, token }, chromium), { status });
    assert.deepEqual(events.contexts, [{ storageState: selection.storage_state, serviceWorkers: 'block' }]);
    assert.deepEqual(events.closed, ['context', 'browser']);
  }
});

test('foreign provider handoff and foreign navigation fail closed', async () => {
  const { chromium, events } = simulatedBrowser({ handoff: { launch_url: 'https://other.example.org/federation?SigninToken=private-ticket' } });
  await assert.rejects(recover({ ...selection, kind: 'allowed' }, chromium), /unexpected handoff/);
  assert.deepEqual(events.closed, ['context', 'browser']);
  const route = { request: () => ({ url: () => 'https://other.example.org/collect', isNavigationRequest: () => true }),
    abort: () => { events.calls.push(['abort']); }, continue: () => { events.calls.push(['continue']); } };
  await events.route(route);
  assert.deepEqual(events.calls.at(-1), ['abort']);
});

test('redirect away from first-party origin fails before the denial fetch', async () => {
  const { chromium, events } = simulatedBrowser({ pageUrl: 'https://other.example.org/' });
  await assert.rejects(recover({ ...selection, kind: 'invalid', token: 'invalid' }, chromium), /not first-party/);
  assert.equal(events.calls.some(([action]) => action === 'fetch'), false);
  assert.deepEqual(events.closed, ['context', 'browser']);
});

test('direct CLI invocation rejects malformed input without printing it', () => {
  const script = relative(process.cwd(), fileURLToPath(new URL('./browser_recovery.mjs', import.meta.url)));
  const result = spawnSync(process.execPath, [script], { input: '{private-ticket', encoding: 'utf8' });
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr, '');
});

test('shared browser boundary blocks AWS, popup and external subresource requests', async () => {
  let handler;
  await restrictToFirstParty({ route: async (_pattern, callback) => { handler = callback; } }, selection.gateway_url);
  for (const [url, expected] of [
    ['https://signin.aws.amazon.com/federation?SigninToken=private-ticket', 'abort'],
    ['https://console.aws.amazon.com/devops-agent/', 'abort'],
    ['https://other.example.org/script.js', 'abort'],
    ['https://gateway.example.org:444/', 'abort'],
    ['http://gateway.example.org/', 'abort'],
    ['https://gateway.example.org/api/v1/products', 'fulfill'],
  ]) {
    const decisions = [];
    const response = { status: () => 200 };
    await handler({ request: () => ({ url: () => url }),
      fetch: async options => { assert.deepEqual(options, { maxRedirects: 0 }); return response; },
      abort: async () => { decisions.push('abort'); },
      fulfill: async options => { assert.equal(options.response, response); decisions.push('fulfill'); } });
    assert.deepEqual(decisions, [expected]);
  }
  await assert.rejects(restrictToFirstParty({}, 'http://gateway.example.org'), /HTTPS/);
});

test('first-party redirects are not followed by the browser or request client', async () => {
  let handler;
  await restrictToFirstParty({ route: async (_pattern, callback) => { handler = callback; } }, selection.gateway_url);
  for (const status of [301, 302, 303, 307, 308]) {
    const events = [];
    await handler({
      request: () => ({ url: () => `${selection.gateway_url}/redirect` }),
      fetch: async options => {
        assert.deepEqual(options, { maxRedirects: 0 });
        events.push('fetch');
        return { status: () => status };
      },
      abort: async () => { events.push('abort'); },
      fulfill: async () => { events.push('fulfill'); },
      continue: async () => { events.push('continue'); },
    });
    assert.deepEqual(events, ['fetch', 'abort']);
  }
});
