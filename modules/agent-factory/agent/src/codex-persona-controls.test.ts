import { startCodexPersonaControls } from './codex-persona-controls';
import { startControlRuntime } from './control-runtime-factory';
import { CodexControlAdapter } from './harnesses/codex-control';
import { PauseGate } from './pause-gate';

jest.mock('./control-runtime-factory', () => ({ startControlRuntime: jest.fn() }));
let adapter: CodexControlAdapter;
let queued: string[];

beforeEach(() => {
  queued = [];
  const gate = new PauseGate({ deadlineAt: () => Date.now() + 600000, backgroundWorkProbe: () => 0, log: () => {} });
  adapter = new CodexControlAdapter(gate);
  (startControlRuntime as jest.Mock).mockResolvedValue({
    runtime: { adapter, gate, steerQueue: {
      flush: async () => { if (queued.length) await adapter.submitInput({ text: queued.shift()! } as any); }, dispose() {},
    } }, listener: { stop: async () => {} },
  });
});

it('fences new effects during pause and resumes the same adapter', async () => {
  const controls = await startCodexPersonaControls(() => {});
  try {
    await controls.checkpoint();
    await adapter.requestPause({ timeoutMs: 2000 });
    expect(adapter.gate.isPauseActive()).toBe(true);
    const effect = jest.fn(async () => 'verified');
    const pending = controls.operation(effect);
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(effect).not.toHaveBeenCalled();
    await adapter.resumeFromPause();
    expect(await pending).toBe('verified');
    expect(effect).toHaveBeenCalledTimes(1);
  } finally { await controls.close(); }
});

it('delivers steering once through the actual host boundary', async () => {
  const controls = await startCodexPersonaControls(() => {});
  try {
    queued.push('Include recovery evidence.');
    await controls.checkpoint();
    expect(controls.takeSteering()).toEqual(['Include recovery evidence.']);
    expect(controls.takeSteering()).toEqual([]);
  } finally { await controls.close(); }
});

it('abort unblocks a paused host without starting the effect', async () => {
  const controls = await startCodexPersonaControls(() => {});
  try {
    await controls.checkpoint();
    await adapter.requestPause({ timeoutMs: 2000 });
    const effect = jest.fn(async () => 'unexpected');
    const pending = controls.operation(effect);
    const refused = expect(pending).rejects.toThrow();
    adapter.cancel('Operator aborted');
    await refused;
    expect(effect).not.toHaveBeenCalled();
  } finally { await controls.close(); }
});

it('an uncertain operation cancels the adapter and cannot be replayed', async () => {
  const controls = await startCodexPersonaControls(() => {});
  try {
    const effect = jest.fn(async () => { throw new Error('outcome unknown'); });
    await expect(controls.operation(effect)).rejects.toThrow('outcome unknown');
    expect(controls.signal.aborted).toBe(true);
    await expect(controls.operation(effect)).rejects.toThrow();
    expect(effect).toHaveBeenCalledTimes(1);
  } finally { await controls.close(); }
});

it('refuses to launch without the existing signed control listener', async () => {
  (startControlRuntime as jest.Mock).mockResolvedValue({ runtime: null, listener: null });
  await expect(startCodexPersonaControls(() => {})).rejects.toThrow('signed control runtime');
});
