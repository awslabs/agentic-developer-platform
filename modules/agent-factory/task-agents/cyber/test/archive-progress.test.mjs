import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { cyberTools } from '../src/driver.mjs';
import { HostBridge } from '../src/protocol.mjs';

const scanId = 'a'.repeat(64);
function harness(results, sleepHook) {
  const frames = [], calls = [];
  const bridge = new HostBridge({task_id: 'tsk_' + randomUUID(), tool_grants: ['cyber.common_crawl_scan', 'cyber.common_crawl_result']}, value => frames.push(value));
  let clock = 100000;
  bridge.cyber = async (operation, payload) => {
    calls.push({operation, payload});
    return {operation_status: 'confirmed', result: {scan_id:scanId, ...results[Math.min(calls.length - 1, results.length - 1)]}};
  };
  const tools = cyberTools(bridge, {now: () => clock, sleep: async (ms, _, {signal}) => {
    if (signal.aborted) throw Error('cancelled');
    await sleepHook?.(frames, calls, bridge);
    if (signal.aborted) throw Error('cancelled');
    clock += ms;
  }});
  return {bridge, frames, calls, scan: tools.find(t => t.name === 'common_crawl_scan'), result: tools.find(t => t.name === 'common_crawl_result'), report: tools.find(t => t.name === 'submit_report')};
}
const pending = query_state => ({status: 'pending', query_state});
const messages = frames => frames.filter(f => f.type === 'progress').map(f => f.message);

test('archive observations reach host frames before the next polling wait', async () => {
  const h = harness([pending('QUEUED'), pending('RUNNING'), pending('RUNNING'), {status:'completed', captures:[{}, {}]}], (frames, calls) => {
    assert.ok(messages(frames).some(m => calls.length === 1 ? m.includes('is queued') : m.includes('Searching Common Crawl')));
    assert.ok(!messages(frames).some(m => m.includes('returned')));
  });
  const result = await h.scan.handler({url:'https://example.com'});
  assert.equal(JSON.parse(result.content[0].text).result.status, 'completed');
  assert.deepEqual(messages(h.frames), ['Starting common crawl scan.', 'Common Crawl archive search is queued.', 'Searching Common Crawl archives for historical captures.', 'Common Crawl returned 2 archived captures for review.']);
  assert.equal(h.calls.filter(c => c.operation === 'common_crawl_scan').length, 1);
  assert.ok(h.frames.every(f => f.task_id === h.bridge.start.task_id && f.report_id && f.producer_timestamp));
  await h.scan.handler({url:'https://example.com'});
  assert.equal(h.frames.length, 4, 'cached submissions do not replay progress');
});

test('long unchanged polls get paced reminders and remain incomplete', async () => {
  const h = harness([{...pending('RUNNING'), bytes_scanned:2500000}]);
  await h.scan.handler({url:'https://example.com'});
  const events = messages(h.frames);
  assert.equal(h.calls.length, 17);
  assert.ok(events.some(m => m.includes('still searching') && m.includes('seconds elapsed')));
  assert.ok(events.some(m => m.includes('2.5 MB')));
  assert.ok(events.length < 10, 'do not publish every poll');
  assert.match(events.at(-1), /still pending after this polling window/);
  assert.ok(!events.some(m => /completed|returned|%/.test(m)));
  assert.equal((await h.report.handler({summary:'Done',findings:[],uncertainties:[],recommendations:[]})).isError, true);
});

for (const [result, expected] of [
  [{status:'completed', captures:[]}, /no matching captures.*does not establish.*safe/],
  [{status:'completed'}, /search completed\.$/],
  [{status:'failed',reason:'query_deadline_exceeded'}, /time limit.*incomplete/],
  [{status:'failed',query_state:'CANCELLED'}, /cancelled.*incomplete/],
  [{status:'failed'}, /failed.*incomplete/],
  [{status:'partial'}, /limited/],
]) {
  test(`terminal observation: ${JSON.stringify(result)}`, async () => {
    const h = harness([result]);
    await h.result.handler({scan_id:scanId});
    assert.match(messages(h.frames).at(-1), expected);
    await h.result.handler({scan_id:scanId});
    assert.equal(h.frames.length, 1, 'repeated terminal reads do not spam');
  });
}

test('cancellation during the wait emits no later work or completion', async () => {
  const h = harness([pending('RUNNING')], (_frames, _calls, bridge) => bridge.controller.abort());
  await assert.rejects(h.scan.handler({url:'https://example.com'}), /cancelled/);
  assert.equal(h.calls.length, 1);
  assert.equal(h.frames.length, 2);
});

test('unconfirmed receipts do not publish query outcomes', async () => {
  const h = harness([]);
  h.bridge.cyber = async () => ({operation_status:'unknown',result:{scan_id:scanId,status:'completed',captures:[{}]}});
  await h.result.handler({scan_id:scanId});
  assert.deepEqual(h.frames, []);
});
