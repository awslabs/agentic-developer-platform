import * as fs from 'node:fs';
import * as path from 'node:path';
import { CLAUDE_SDK_VERSION } from '../harnesses/claude-control';
import { REQUEST_SHAPE_NORMALIZATION } from './canonical-json';
import { PROBE_PROMPT_SHA256 } from './request-shape';

const agentManifest = path.join(__dirname, 'request-shape-manifest.json');
const gatewayManifest = path.resolve(
  __dirname, '..', '..', '..', '..', 'gateway', 'src', 'admin', 'persona_models', 'request-shape-manifest.json',
);

/**
 * The gateway selects the expected digest from its copy and the runner refuses a
 * claim whose digest differs from its own copy, so the two files must be the
 * same bytes and must have been generated under the normalization revision and
 * SDK revision this runtime implements (#7993).
 */
describe('request-shape manifest copies', () => {
  it('are byte-identical between the agent runtime and the gateway', () => {
    expect(fs.readFileSync(gatewayManifest, 'utf8')).toBe(fs.readFileSync(agentManifest, 'utf8'));
  });

  it('were generated under the normalization, SDK and prompt this runtime implements', () => {
    const parsed = JSON.parse(fs.readFileSync(agentManifest, 'utf8'));
    expect(parsed.schema_version).toBe(2);
    expect(parsed.request_shape_normalization).toBe(REQUEST_SHAPE_NORMALIZATION);
    expect(parsed.harness_contract_revision).toBe(CLAUDE_SDK_VERSION);
    expect(parsed.probe_prompt_sha256).toBe(PROBE_PROMPT_SHA256);
    expect(Object.keys(parsed.models).length).toBeGreaterThan(0);
    for (const digest of Object.values(parsed.models)) expect(digest).toMatch(/^[0-9a-f]{64}$/);
  });
});
