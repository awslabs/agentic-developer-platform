#!/usr/bin/env node
import { runProbeCycle } from './runner';

export function assertLocalProbeEnabled(env: NodeJS.ProcessEnv = process.env): void {
  if (env.ADP_PERSONA_MODEL_PROBE_ENABLED !== 'true') {
    throw new Error('ADP persona-model probe is locally disabled');
  }
}

async function main(): Promise<void> {
  assertLocalProbeEnabled();
  const result = await runProbeCycle();
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`[invocability-probe] fatal: ${(error as Error).message}`);
    process.exitCode = 1;
  });
}
