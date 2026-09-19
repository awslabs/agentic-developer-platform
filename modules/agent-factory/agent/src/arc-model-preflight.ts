/** Read-only authority preflight; each actual SDK launch obtains its own decision. */
import { prepareModelQuery } from './model-policy-runtime';

async function main() {
  if (process.env.ADP_ARC_MODEL_POLICY_ENABLED !== 'true') throw new Error('ARC policy preflight must be explicitly enabled');
  await prepareModelQuery({ prompt: '', options: { model: process.env.ANTHROPIC_MODEL } });
  console.info('ARC model authority preflight passed');
}
main().catch(() => { console.error('ARC model authority preflight refused'); process.exitCode = 1; });
