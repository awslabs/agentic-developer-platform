import test from 'node:test';
import assert from 'node:assert/strict';
import { cleanupCodexProcess } from '../codex-runner.mjs';

test('cleanup terminates surviving descendants before deleting the home, with ENOTEMPTY retries', async () => {
  const events = [];
  await cleanupCodexProcess({ pid: 123 }, '/private/task', {
    signal: (pid, signal) => { assert.equal(pid, -123); events.push(signal); },
    wait: async () => {},
    remove: async (path, options) => { events.push('remove'); assert.equal(path, '/private/task'); assert.ok(options.maxRetries > 0); assert.ok(options.retryDelay > 0); },
  });
  assert.equal(events[0], 'SIGTERM');
  assert.deepEqual(events.slice(-2), ['SIGKILL', 'remove']);
});

test('already exited process group still removes temporary home', async () => {
  let removed = false;
  await cleanupCodexProcess({ pid: 123 }, '/private/task', {
    signal: () => { throw Object.assign(new Error('gone'), { code: 'ESRCH' }); },
    remove: async () => { removed = true; },
  });
  assert.ok(removed);
});

test('unexpected termination errors are not hidden by removing a live workspace', async () => {
  await assert.rejects(cleanupCodexProcess({ pid: 123 }, '/private/task', {
    signal: () => { throw Object.assign(new Error('denied'), { code: 'EPERM' }); },
    remove: async () => assert.fail('must not delete while process is live'),
  }), { code: 'EPERM' });
});

test('cleanup removes a workspace after stopping an orphaned file-writing child', async () => {
  const { spawn } = await import('node:child_process');
  const { mkdtemp, access, rm } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const { once } = await import('node:events');
  const { setTimeout: wait } = await import('node:timers/promises');
  const home = await mkdtemp(join(tmpdir(), 'adp-cleanup-test-'));
  // The leader exits while a descendant keeps recreating plugin-clone files.
  const writer = `const fs=require('node:fs'); const p=${JSON.stringify(home)}; setInterval(()=>{fs.mkdirSync(p+'/plugins',{recursive:true});fs.writeFileSync(p+'/plugins/data','x');},10);`;
  const leader = spawn(process.execPath, ['-e', `require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(writer)}],{stdio:'ignore'}).unref()`], { detached: true, stdio: 'ignore' });
  try {
    await once(leader, 'close');
    await wait(100);
    await cleanupCodexProcess(leader, home);
    await wait(100);
    await assert.rejects(access(home), { code: 'ENOENT' });
  } finally {
    try { process.kill(-leader.pid, 'SIGKILL'); } catch {}
    await rm(home, { recursive: true, force: true, maxRetries: 10 });
  }
});
