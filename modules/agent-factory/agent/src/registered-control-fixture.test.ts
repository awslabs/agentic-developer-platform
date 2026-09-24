import { mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { createServer } from 'net';
import { resilientQuery } from './utils/resilientQuery';
import { runRegisteredControlFixture } from './registered-control-fixture';
jest.mock('./utils/resilientQuery', () => ({ resilientQuery: jest.fn() }));

async function freePort(): Promise<number> {
  const server = createServer();
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (!address || typeof address === 'string') throw new Error('no port');
      server.close(() => resolve(address.port));
    });
  });
}

describe('registered control fixture with the real runtime and substituted SDK transport', () => {
  let original: NodeJS.ProcessEnv;
  let dir: string;
  let interrupt: jest.Mock;
  let close: jest.Mock;
  beforeEach(async () => {
    original = process.env;
    dir = mkdtempSync(join(tmpdir(), 'registered-control-'));
    process.env = { ...original,
      FEATURE_AGENT_CONTROL_ENABLED: 'true', ADP_CONTROL_BIND_ADDRESS: '127.0.0.1',
      ADP_CONTROL_PORT: String(await freePort()), ADP_CONTROL_TOKEN: 'fixture-test-token-0123456789abcdef',
      ADP_CONTROL_TOKEN_EXPIRES_AT: new Date(Date.now() + 60_000).toISOString(),
      ADP_CONTROL_GENERATION: '1', ADP_CONTROL_RUN_ID: 'invocation-test', ADP_MESSAGE_ID: 'invocation-test',
      W2_FIXTURE_RUN_ID: 'w2-test', ADP_CONTROL_FIXTURE_MODE: 'native-interrupt',
      ADP_CONTROL_FIXTURE_OUTPUT: join(dir, 'runtime.json'), ADP_CONTROL_FIXTURE_SOURCE_REVISION: 'a'.repeat(40),
    };
    interrupt = jest.fn().mockResolvedValue(undefined);
    close = jest.fn();
  });
  afterEach(() => { process.env = original; rmSync(dir, { recursive: true, force: true }); jest.clearAllMocks(); });

  function transport(withAssistant = true, fail = false): void {
    (resilientQuery as jest.Mock).mockImplementation(async function* (args) {
      const input = args.attemptInputFactory({ attemptNumber: 1, isResume: false, promptText: args.queryParams.prompt });
      try {
        await args.onAttemptHandle({ attemptNumber: 1, session: { interrupt, close } });
        if (withAssistant) yield { type: 'assistant', message: { content: [] } };
        if (fail) throw new Error('transport disconnected');
        yield { type: 'result', subtype: 'success', is_error: false };
      } finally { await input.dispose(); }
    });
  }

  it('calls native interruption on the same active SDK handle and preserves its invocation identity', async () => {
    transport();
    expect(await runRegisteredControlFixture()).toBe(0);
    expect(interrupt).toHaveBeenCalledTimes(1);
    const report = JSON.parse(readFileSync(join(dir, 'runtime.json'), 'utf8'));
    expect(report.invocation_id).toBe('invocation-test');
    expect(report.native_acknowledged).toBe(true);
    const types = report.events.map((event: { type: string }) => event.type);
    expect(types.indexOf('sdk_message')).toBeLessThan(types.indexOf('native_interrupt_requested'));
    expect(types.indexOf('native_interrupt_acknowledged')).toBeLessThan(types.indexOf('sdk_result'));
    expect(types.at(-1)).toBe('runtime_disposed');
    expect(readFileSync(join(dir, 'runtime.json'), 'utf8')).not.toContain(process.env.ADP_CONTROL_TOKEN);
    expect((resilientQuery as jest.Mock).mock.calls[0][0].maxRetries).toBe(0);
  });
  it('never substitutes completion before an active turn for native interruption evidence', async () => {
    transport(false);
    expect(await runRegisteredControlFixture()).toBe(1);
    expect(interrupt).not.toHaveBeenCalled();
  });
  it('does not initiate native interruption in the operator-control scenario', async () => {
    process.env.ADP_CONTROL_FIXTURE_MODE = 'registered-control';
    transport();
    expect(await runRegisteredControlFixture()).toBe(0);
    expect(interrupt).not.toHaveBeenCalled();
  });
  it('retains a transport failure and disposes the runtime', async () => {
    transport(true, true);
    expect(await runRegisteredControlFixture()).toBe(1);
    const report = JSON.parse(readFileSync(join(dir, 'runtime.json'), 'utf8'));
    expect(report.result_seen).toBe(false);
    expect(report.events.at(-1).type).toBe('runtime_disposed');
  });
  it('refuses foreign invocation identity before starting a query', async () => {
    process.env.ADP_CONTROL_RUN_ID = 'other-invocation';
    await expect(runRegisteredControlFixture()).rejects.toThrow('identity');
    expect(resilientQuery).not.toHaveBeenCalled();
  });
});
