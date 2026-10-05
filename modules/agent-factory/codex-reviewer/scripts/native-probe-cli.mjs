import { readFile } from 'node:fs/promises';
import { captureNativeProbe } from './capture-native-probe.mjs';
import { nativeProbeBody, NATIVE_PROBE_SDK, NATIVE_PROBE_NORMALIZATION } from './native-probe-shape.mjs';
const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url)));
if (pkg.dependencies['@openai/codex-sdk'] !== NATIVE_PROBE_SDK) throw new Error('Native probe SDK version changed');
const [persona, model] = process.argv.slice(2);
const manifest = JSON.parse(await readFile(new URL('./native-probe-manifest.json', import.meta.url)));
if (manifest.sdk !== NATIVE_PROBE_SDK || manifest.normalization !== NATIVE_PROBE_NORMALIZATION ||
    !manifest.personas[persona]?.[model]) throw new Error('Unknown native probe contract');
const captured = nativeProbeBody(await captureNativeProbe(model, persona.replace(/^agent-codex-/, '')));
if (captured.digest !== manifest.personas[persona][model]) throw new Error('Native SDK request shape changed');
process.stdout.write(JSON.stringify(captured));
