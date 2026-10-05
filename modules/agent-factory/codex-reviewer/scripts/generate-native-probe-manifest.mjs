/** Generate hashes from two fresh actual SDK captures per native persona/model. */
import { readFile, writeFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { captureNativeProbe } from './capture-native-probe.mjs';
import { nativeProbeBody, NATIVE_PROBE_SDK, NATIVE_PROBE_NORMALIZATION } from './native-probe-shape.mjs';
const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url)));
assert.equal(pkg.dependencies['@openai/codex-sdk'], NATIVE_PROBE_SDK);
const manifest = { schema_version: 1, sdk: NATIVE_PROBE_SDK, normalization: NATIVE_PROBE_NORMALIZATION, personas: {} };
for (const persona of ['developer', 'reviewer']) {
  const models = {};
  for (const model of ['openai.gpt-6-astra', 'openai.gpt-6-sol', 'openai.gpt-6-luna']) {
    const first = nativeProbeBody(await captureNativeProbe(model, persona));
    const second = nativeProbeBody(await captureNativeProbe(model, persona));
    assert.equal(first.digest, second.digest, `Non-reproducible ${persona}/${model}`);
    models[model] = first.digest;
    console.error(`Captured ${persona}/${model}`);
  }
  manifest.personas[`agent-codex-${persona}`] = models;
}
const outputs = ['./native-probe-manifest.json', '../../agent/src/invocability-probe/native-probe-manifest.json',
  '../../../gateway/src/admin/persona_models/native-probe-manifest.json'].map(path => new URL(path, import.meta.url));
const serialized = JSON.stringify(manifest, null, 2) + '\n';
for (const output of outputs) {
  if (process.argv.includes('--check')) assert.equal(await readFile(output, 'utf8'), serialized);
  else await writeFile(output, serialized);
}
