import assert from 'node:assert/strict';
import test from 'node:test';
import {handler} from './index.mjs';

test('emits actual synthetic failure and recovery records with the same bounded correlation', async () => {
  const records = [];
  const original = console.log;
  console.log = value => records.push(JSON.parse(value));
  try {
    const event = {schema_version: 1, correlation_id: '0000000000000001', scenario: 'dependency_timeout'};
    await assert.rejects(handler(event), /SYNTHETIC_DEPENDENCY_TIMEOUT/);
    const result = await handler({...event, scenario: 'recovered'});
    assert.equal(result.code, 'SYNTHETIC_RECOVERY');
    assert.deepEqual(records.map(record => record.outcome), ['failed', 'recovered']);
    assert.ok(records.every(record => record.synthetic && record.correlation_id === event.correlation_id));
  } finally {
    console.log = original;
  }
});

test('rejects arbitrary or secret-bearing inputs before producing logs', async () => {
  const original = console.log;
  let logged = false;
  console.log = () => { logged = true; };
  try {
    for (const event of [null, {}, {schema_version: 1, correlation_id: 'url-or-secret', scenario: 'recovered'},
      {schema_version: 1, correlation_id: '0000000000000001', scenario: 'recovered', token: 'not-accepted'}]) {
      await assert.rejects(handler(event), /fixture_input_invalid/);
    }
    assert.equal(logged, false);
  } finally {
    console.log = original;
  }
});
