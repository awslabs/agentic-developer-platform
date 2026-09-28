#!/usr/bin/env node
import { runProbeCycle } from './runner';
import { runTaskProbe } from './task-runner';
import { runNativeProbe } from './native-runner';

export function assertLocalProbeEnabled(env: NodeJS.ProcessEnv = process.env): void {
  if (env.ADP_PERSONA_MODEL_PROBE_ENABLED !== 'true') {
    throw new Error('ADP persona-model probe is locally disabled');
  }
}

async function main(): Promise<void> {
  assertLocalProbeEnabled();
  if (process.env.ADP_NATIVE_PROBE_PERSONA && process.env.ADP_TASK_PROBE_PERSONA) throw new Error('Choose one probe profile');
  const result = process.env.ADP_NATIVE_PROBE_PERSONA
    ? await runNativeProbe(process.env.ADP_NATIVE_PROBE_PERSONA)
    : process.env.ADP_TASK_PROBE_PERSONA
    ? await runTaskProbe(process.env.ADP_TASK_PROBE_PERSONA)
    : await runProbeCycle();
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`[invocability-probe] fatal: ${(error as Error).message}`);
    process.exitCode = 1;
  });
}
