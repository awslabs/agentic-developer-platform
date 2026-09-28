import * as net from 'net';
import { ExplanationEvents, MAX_SUBSCRIBERS, HISTORY_EVENTS, EVENT_BYTES } from './explanation-events';
import { ControlListener } from './control-listener';
import { ControlStateStore } from './control-state';

describe('bounded explanation history', () => {
  it('replays in order and identifies expired, foreign and future cursors', () => {
    const hub = new ExplanationEvents('run', 2);
    for (let n = 0; n < 200; n++) hub.publish(`explanation ${n}`);
    expect(hub.replay().events).toHaveLength(HISTORY_EVENTS);
    expect(hub.replay().reset).toBe(true);
    expect(hub.replay('run:2:199').events.map(e => e.sequence)).toEqual([200]);
    for (const cursor of ['run:2:1', 'run:1:199', 'other:2:199', 'run:2:999']) expect(hub.replay(cursor).reset).toBe(true);
  });
  it('bounds UTF8 and escaped payloads, isolates throwing subscribers, and ends once', () => {
    const hub = new ExplanationEvents('run', 1);
    hub.subscribe(() => { throw new Error('broken client'); });
    hub.publish('界\u0000'.repeat(20000)); hub.publish('still running'); hub.finish(); hub.finish(); hub.publish('too late');
    for (const e of hub.replay().events) expect(Buffer.byteLength(JSON.stringify(e))).toBeLessThanOrEqual(EVENT_BYTES);
    expect(hub.replay().events.map(e => e.kind)).toEqual(['explanation', 'explanation', 'terminal']);
  });
  it('withholds credential-like authored content', () => {
    const hub = new ExplanationEvents('run', 1);
    hub.publish('My key is AKIAABCDEFGHIJKLMNOP');
    expect(hub.replay().events[0].payload.text).toContain('omitted');
    expect(JSON.stringify(hub.replay())).not.toContain('AKIA');
  });
  it('limits subscriptions and frees slots', () => {
    const hub = new ExplanationEvents('run', 1);
    const release = Array.from({ length: MAX_SUBSCRIBERS }, () => hub.subscribe(() => undefined));
    expect(() => hub.subscribe(() => undefined)).toThrow();
    release[0](); expect(() => hub.subscribe(() => undefined)).not.toThrow();
  });
});

describe('real read-only listener', () => {
  let listener: ControlListener, port: number, hub: ExplanationEvents;
  beforeEach(async () => {
    const socket = net.createServer();
    await new Promise<void>(resolve => socket.listen(0, '127.0.0.1', resolve));
    port = (socket.address() as net.AddressInfo).port;
    await new Promise<void>(resolve => socket.close(() => resolve()));
    hub = new ExplanationEvents('run', 1);
    listener = new ControlListener({ bindAddress: '127.0.0.1', port, token: 'secret',
      tokenExpiresAt: new Date(Date.now() + 60000).toISOString(), generation: 1, runId: 'run',
      store: new ControlStateStore({ generation: 1 }), events: hub, logger: () => undefined });
    expect((await listener.start({ FEATURE_AGENT_EXPLANATIONS_ENABLED: 'true' })).started).toBe(true);
  });
  afterEach(async () => { await listener.stop(); });
  function request(path: string, method = 'GET', auth = true) {
    return fetch(`http://127.0.0.1:${port}${path}`, { method,
      headers: auth ? { Authorization: 'Bearer secret', 'X-Adp-Control-Generation': '1' } : {} });
  }
  it('delivers two authored markers before completion with mutation disabled', async () => {
    const response = await request('/agent/events');
    const reader = response.body!.getReader();
    hub.publish('Mechanism: a bounded replay buffer.');
    const first = new TextDecoder().decode((await reader.read()).value);
    expect(first).toContain('Mechanism:');
    hub.publish('Evidence: both markers arrived before terminal.');
    expect(new TextDecoder().decode((await reader.read()).value)).toContain('Evidence:');
    expect((await request('/agent/pause', 'POST')).status).toBe(503);
    expect((await (await request('/agent/state')).json() as any).capabilities.pause).toBe(false);
    await reader.cancel();
  });
  it('refuses missing auth and wrong generation', async () => {
    expect((await request('/agent/events', 'GET', false)).status).toBe(401);
    expect((await fetch(`http://127.0.0.1:${port}/agent/events`, { headers: {
      Authorization: 'Bearer secret', 'X-Adp-Control-Generation': '2',
    } })).status).toBe(401);
  });
  it('closes subscribed connections during stop', async () => {
    const response = await request('/agent/events');
    const reader = response.body!.getReader();
    await listener.stop();
    expect(new TextDecoder().decode((await reader.read()).value)).toContain('terminal');
    expect((await reader.read()).done).toBe(true);
  });
});
