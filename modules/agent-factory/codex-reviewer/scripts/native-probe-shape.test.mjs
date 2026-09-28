import test from 'node:test';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { nativeProbeBody } from './native-probe-shape.mjs';
import { captureNativeProbe } from './capture-native-probe.mjs';

for (const persona of ['developer', 'reviewer']) {
  test(`pinned native ${persona} captures are reproducible and retain semantic fields`, { timeout: 60000 }, async () => {
    const first = await captureNativeProbe('openai.gpt-6-sol', persona);
    const second = await captureNativeProbe('openai.gpt-6-sol', persona);
    const expected = nativeProbeBody(first);
    assert.equal(nativeProbeBody(second).digest, expected.digest);
    const manifest = JSON.parse(await readFile(new URL('./native-probe-manifest.json', import.meta.url)));
    if (expected.digest !== manifest.personas[`agent-codex-${persona}`]['openai.gpt-6-sol']) {
      const directory = join(process.env.RUNNER_TEMP || tmpdir(), 'adp-native-probe-diagnostics');
      await mkdir(directory, { recursive: true });
      // Synthetic local probe context only: no user task, provider output or credentials.
      await writeFile(join(directory, `${persona}.json`), expected.body);
    }
    assert.equal(expected.digest, manifest.personas[`agent-codex-${persona}`]['openai.gpt-6-sol']);
    for (const timezone of ['Etc/UTC', '/UTC', 'UTC', 'America/New_York']) {
      const changed = structuredClone(first);
      const environment = changed.body.input.flatMap(message => message.content).find(part => part.text?.startsWith('<environment_context>'));
      environment.text = environment.text.replace(/<timezone>[^<]+<\/timezone>/, `<timezone>${timezone}</timezone>`);
      if (timezone === 'America/New_York') assert.notEqual(nativeProbeBody(changed).digest, expected.digest);
      else assert.equal(nativeProbeBody(changed).digest, expected.digest);
    }
    const body = JSON.parse(expected.body);
    assert.equal(body.model, 'openai.gpt-6-sol');
    assert.equal(body.stream, false);
    assert.equal(body.max_output_tokens, 512);
    assert.ok(body.tools.some(tool => tool.name === 'exec_command'));
    if (persona === 'reviewer') assert.equal(body.text.format.type, 'json_schema');
    for (const mutate of [
      value => { value.body.tools[0].description += ' changed'; },
      value => { value.body.instructions += ' changed'; },
      value => { value.body.reasoning.effort = 'low'; },
      value => { value.body.input[0].content[0].text += ' changed'; },
      value => { value.body.model = 'openai.gpt-6-astra'; },
      value => { value.body.extra_semantic_field = true; },
      ...(persona === 'reviewer' ? [value => { value.body.text.format.schema.properties.summary.type = 'number'; }] : []),
    ]) {
      const changed = structuredClone(first);
      mutate(changed);
      assert.notEqual(nativeProbeBody(changed).digest, expected.digest);
    }
  });
}
