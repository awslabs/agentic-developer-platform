import { readFile, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { captureReportProbe } from './report-probe.mjs';
const source = new URL('../../../gateway/src/agentauth/codex-github-catalogue.json', import.meta.url);
const catalogue = await readFile(source);
const target = new URL('./report-probe-catalogue.json', import.meta.url);
if (process.argv.includes('--check')) assert.deepEqual(await readFile(target), catalogue);
else await writeFile(target, catalogue);
const manifest = { schema_version: 1, sdk: '0.155.1', normalization: 'codex-report-probe-v1', personas: {} };
for (const name of ['architect', 'product', 'pm', 'intent-refinement']) {
  const persona = 'agent-codex-' + name;
  manifest.personas[persona] = {};
  for (const model of ['openai.gpt-6-astra', 'openai.gpt-6-sol', 'openai.gpt-6-luna']) {
    const first = await captureReportProbe(persona, model), second = await captureReportProbe(persona, model);
    assert.equal(first.digest, second.digest, `Non-reproducible ${persona}/${model}`);
    manifest.personas[persona][model] = first.digest;
    console.error(`Captured ${persona}/${model}`);
  }
}
for (const name of ['./report-probe-manifest.json', '../../agent/src/invocability-probe/report-probe-manifest.json', '../../../gateway/src/admin/persona_models/report-probe-manifest.json']) {
  const output = new URL(name, import.meta.url), serialized = JSON.stringify(manifest, null, 2) + '\n';
  if (process.argv.includes('--check')) assert.equal(await readFile(output, 'utf8'), serialized);
  else await writeFile(output, serialized);
}
