/** Shared signed controls at host model/tool boundaries, never repository hooks. */
import { request } from 'node:http';
import { randomUUID } from 'node:crypto';
import { CodexControlAdapter } from './harnesses/codex-control';
import { startControlRuntime, type ControlLogger } from './control-runtime-factory';

export async function startCodexPersonaControls(log: ControlLogger, deadline?: AbortSignal) {
  const control = await startControlRuntime({ log, createAdapter: gate => new CodexControlAdapter(gate) });
  const adapter = control.runtime?.adapter;
  if (!adapter) {
    control.events?.finish();
    await control.listener?.stop();
    throw new Error('Shared Codex personas require the signed control runtime');
  }
  adapter.drainSteering = () => control.runtime!.steerQueue.flush();
  await adapter.start();
  const expire = () => adapter.cancel('Persona deadline expired');
  deadline?.addEventListener('abort', expire, { once: true });
  if (deadline?.aborted) expire();
  const amendments: string[] = [];
  let ended = false;
  async function boundary(event: 'PreToolUse' | 'PostToolUse' | 'Stop', id?: string) {
    adapter!.signal.throwIfAborted();
    const value = await new Promise<Record<string, any>>((resolve, reject) => {
      const req = request({ socketPath: adapter!.socket, path: '/', method: 'POST',
        signal: adapter!.signal, headers: { 'content-type': 'application/json' } }, res => {
        let body = '';
        res.on('data', chunk => {
          body += chunk;
          if (Buffer.byteLength(body) > 65536) req.destroy(new Error('Control response exceeds bound'));
        });
        res.on('error', reject);
        res.on('end', () => { try { resolve(JSON.parse(body)); } catch { reject(new Error('Invalid host control response')); } });
      });
      req.on('error', reject);
      req.end(JSON.stringify({ hook_event_name: event, tool_use_id: id, tool_name: 'ADPHostOperation',
        ...(event === 'PostToolUse' ? { tool_response: { completed: true } } : {}) }));
    });
    adapter!.signal.throwIfAborted();
    if (value.decision === 'block' && event !== 'Stop') throw new Error('Host operation blocked');
    const steering = event === 'Stop' && value.decision === 'block' ? value.reason : value.hookSpecificOutput?.additionalContext;
    if (typeof steering === 'string' && steering.trim()) amendments.push(steering);
  }
  return {
    signal: adapter.signal,
    async checkpoint() { await boundary('Stop'); },
    takeSteering() { return amendments.splice(0); },
    pendingSteering() { return amendments.length > 0; },
    explain(text: string) { control.events?.publish(text); },
    async operation<T>(execute: () => Promise<T>): Promise<T> {
      const id = randomUUID();
      await boundary('PreToolUse', id);
      try {
        const result = await execute();
        await boundary('PostToolUse', id);
        return result;
      } catch (error) {
        // An unknown effect is never advertised as an idle, safely paused run.
        adapter.cancel('Host operation failed or requires reconciliation');
        throw error;
      }
    },
    async close() {
      if (ended) return;
      ended = true;
      deadline?.removeEventListener('abort', expire);
      control.events?.finish();
      control.runtime!.steerQueue.dispose('Codex persona ended');
      await adapter.dispose();
      await control.listener?.stop();
    },
  };
}
