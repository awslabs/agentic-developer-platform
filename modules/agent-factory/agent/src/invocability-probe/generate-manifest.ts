#!/usr/bin/env node
/**
 * Generate request-shape evidence using the pinned SDK and a loopback-only fake
 * Bedrock upstream. This program cannot make a provider call: the only
 * upstream URL it constructs is a freshly-bound 127.0.0.1 HTTP server.
 *
 * It prints the manifest to stdout. Deliberately review and check it in with
 * apply_patch; generation never overwrites source files.
 */
import * as http from 'node:http';
import { resilientQuery } from '../utils/resilientQuery';
import { CLAUDE_SDK_VERSION } from '../harnesses/claude-control';
import { startCaptureProxy } from './capture-proxy';
import { REQUEST_SHAPE_NORMALIZATION } from './canonical-json';
import {
  PROBE_PROMPT,
  PROBE_PROMPT_SHA256,
  probeSdkEnvironment,
  probeSdkOptions,
} from './request-shape';

const DEFAULT_MODELS = [
  'global.anthropic.claude-opus-5',
  'global.anthropic.claude-opus-4-8',
  'global.anthropic.claude-opus-4-7',
  'global.anthropic.claude-opus-4-6-v1',
  'global.anthropic.claude-opus-4-5-20251101-v1:0',
  'global.anthropic.claude-sonnet-4-6',
  'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  'us.anthropic.claude-sonnet-4-6',
];

async function fakeUpstream(): Promise<{ origin: string; close(): Promise<void> }> {
  const server = http.createServer(async (request, response) => {
    for await (const _chunk of request) { /* consume */ }
    response.writeHead(400, {
      'content-type': 'application/json',
      'x-amzn-requestid': 'local-manifest-generation',
    });
    response.end(JSON.stringify({ __type: 'LocalManifestCapture', message: 'expected fake response' }));
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve());
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fake upstream failed to bind');
  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

async function captureFor(modelId: string): Promise<{ digest: string; body: unknown }> {
  const fake = await fakeUpstream();
  const controller = new AbortController();
  let emittedBody: unknown;
  const proxy = await startCaptureProxy({
    modelId,
    region: 'us-east-1',
    credentials: { accessKeyId: 'LOCALONLY', secretAccessKey: 'LOCALONLY' },
    upstreamBaseUrl: fake.origin,
    signal: controller.signal,
    onCapturedBody: (body) => { emittedBody = body; },
  });
  const env = probeSdkEnvironment({
    baseUrl: proxy.baseUrl,
    region: 'us-east-1',
    accessKeyId: 'LOCALONLY',
    secretAccessKey: 'LOCALONLY',
    sessionToken: 'LOCALONLY',
  });
  const consume = (async () => {
    try {
      for await (const _message of resilientQuery({
        queryParams: {
          prompt: PROBE_PROMPT,
          options: probeSdkOptions({ modelId, maxBudgetUsd: 0.01, abortController: controller, env }),
        },
        maxRetries: 0,
        idleTimeoutMs: 15_000,
        log: (line) => console.error(`[manifest:${modelId}] ${line}`),
      })) { /* consume until the expected fake error */ }
    } catch { /* the local upstream deliberately returns an SDK error */ }
  })();
  try {
    const captured = await Promise.race([
      proxy.captured(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('SDK emitted no Bedrock request')), 15_000)),
    ]);
    if (emittedBody === undefined) throw new Error('capture proxy did not expose the SDK body');
    return { digest: captured.requestShapeSha256, body: emittedBody };
  } finally {
    controller.abort();
    await consume;
    await proxy.close();
    await fake.close();
  }
}

function differingPaths(left: unknown, right: unknown, path = '$'): string[] {
  if (Object.is(left, right)) return [];
  if (Array.isArray(left) && Array.isArray(right)) {
    if (left.length !== right.length) return [`${path}.length`];
    return left.flatMap((value, index) => differingPaths(value, right[index], `${path}[${index}]`));
  }
  if (left && right && typeof left === 'object' && typeof right === 'object') {
    const a = left as Record<string, unknown>;
    const b = right as Record<string, unknown>;
    const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
    return keys.flatMap((key) => differingPaths(a[key], b[key], `${path}.${key}`));
  }
  return [path];
}

async function main(): Promise<void> {
  const models = process.argv.slice(2);
  const selected = models.length > 0 ? models : DEFAULT_MODELS;
  const digests: Record<string, string> = {};
  for (const model of selected) {
    // Generate twice. A manifest derived from a volatile body would create an
    // unsafe runtime that either rejects every call or blesses the wrong body.
    const first = await captureFor(model);
    const second = await captureFor(model);
    if (first.digest !== second.digest) {
      throw new Error(`non-deterministic SDK request for ${model}: ${differingPaths(first.body, second.body).join(', ')}`);
    }
    if (process.env.ADP_PROBE_PRINT_BODY === '1') {
      console.error(`[manifest-body:${model}] ${JSON.stringify(first.body)}`);
    }
    digests[model] = first.digest;
  }
  process.stdout.write(`${JSON.stringify({
    schema_version: 2,
    request_shape_normalization: REQUEST_SHAPE_NORMALIZATION,
    compatibility_class: 'claude-agent-sdk',
    harness_contract_revision: CLAUDE_SDK_VERSION,
    probe_prompt_sha256: PROBE_PROMPT_SHA256,
    models: digests,
  }, null, 2)}\n`);
}

if (require.main === module) {
  main().catch((error) => {
    console.error((error as Error).stack ?? String(error));
    process.exitCode = 1;
  });
}
