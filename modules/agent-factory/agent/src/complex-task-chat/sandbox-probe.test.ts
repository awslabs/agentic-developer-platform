import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';

const root = join(__dirname, '..', '..');
const smoke = join(root, 'scripts', 'chat-sandbox-probe-image.test.sh');
const probe = join(root, 'scripts', 'chat-sandbox-probe.sh');
const image = `sha256:${'a'.repeat(64)}`;
const clients = [
  '@aws-sdk/credential-provider-node', '@aws-sdk/client-sts', '@aws-sdk/client-s3',
  '@aws-sdk/client-dynamodb', '@aws-sdk/client-secrets-manager', '@aws-sdk/client-bedrock-runtime',
];

describe('restricted-image probe prerequisites (offline contracts, not isolation evidence)', () => {
  let directory: string;
  let log: string;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), 'chat-probe-contract-'));
    log = join(directory, 'docker-arguments');
    writeFileSync(join(directory, 'docker'), `#!/bin/bash
printf '%s\\0' "$@" >> "$DOCKER_FIXTURE_LOG"
if [[ "$1" == image ]]; then exit "\${DOCKER_FIXTURE_INSPECT_STATUS:-0}"; fi
exit "\${DOCKER_FIXTURE_RUN_STATUS:-0}"
`, { mode: 0o700 });
  });
  afterEach(() => rmSync(directory, { recursive: true, force: true }));

  function invoke(reference = image, changes: NodeJS.ProcessEnv = {}) {
    return spawnSync('/bin/bash', [smoke, reference], {
      encoding: 'utf8', timeout: 10000,
      env: { ...process.env, PATH: directory, DOCKER_FIXTURE_LOG: log, ...changes },
    });
  }

  it('includes real probe clients and the probe executable in the production image source', () => {
    const dockerfile = readFileSync(join(root, 'Dockerfile.sandbox'), 'utf8');
    expect(dockerfile).toContain('apk add --no-cache bash curl aws-cli');
    expect(dockerfile).toContain('COPY --chown=agent:agent agent/scripts/chat-sandbox-probe.sh ./chat-sandbox-probe');
    expect(dockerfile).toContain('chmod +x ./chat-sandbox-entrypoint ./chat-sandbox-probe');
    const ignores = readFileSync(join(root, 'Dockerfile.sandbox.dockerignore'), 'utf8').split('\n');
    expect(ignores).toEqual(expect.arrayContaining(['!agent/scripts/', '!agent/scripts/chat-sandbox-probe.sh']));
    const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
    const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
    for (const client of clients) {
      expect(manifest.dependencies[client]).toBe(lock.packages[''].dependencies[client]);
      expect(lock.packages[`node_modules/${client}`].integrity).toMatch(/^sha512-/);
      expect(require(client)).toBeDefined();
      expect(readFileSync(smoke, 'utf8')).toContain(`"${client}"`);
    }
    expect(lock.packages['node_modules/@aws-sdk/client-sts'].version).toBe(manifest.dependencies['@aws-sdk/client-sts']);
  });

  it('parses both shell commands and includes direct Bedrock CLI and SDK probes', () => {
    for (const script of [probe, smoke]) {
      expect(spawnSync('/bin/bash', ['-n', script]).status).toBe(0);
    }
    const source = readFileSync(probe, 'utf8');
    expect(source).toContain('bedrock-runtime) arguments=(invoke-model');
    expect(source).toContain("'@aws-sdk/client-bedrock-runtime', 'BedrockRuntimeClient', 'InvokeModelCommand'");
    const sdk = source.split("<<'NODE'\n")[1].split('\nNODE\n')[0];
    expect(spawnSync(process.execPath, ['--check'], { input: sdk, encoding: 'utf8' }).status).toBe(0);
  });

  it('retains the probe refusal before SDK, CLI or network operations on the worker', () => {
    const result = spawnSync('/bin/bash', [probe], {
      env: { ...process.env, ADP_PROBE_AUTHORIZED: 'false' }, encoding: 'utf8', timeout: 5000,
    });
    expect(result.status).toBe(3);
    expect(result.stderr).toContain('Sandbox probe refused');
    expect(result.stdout).toBe('');
  });

  it('loads SDKs from the installed probe instead of the sandbox working directory', async () => {
    const loaded: string[] = [];
    const reports: string[] = [];
    class DeniedClient {
      async send() { throw { name: 'CredentialsProviderError' }; }
      destroy() {}
    }
    const loadSdk = (name: string) => {
      loaded.push(name);
      if (name === '@aws-sdk/credential-provider-node') {
        return { defaultProvider: () => async () => { throw { name: 'CredentialsProviderError' }; } };
      }
      return new Proxy({}, { get: (_target, name) => String(name).endsWith('Client') ? DeniedClient : class {} });
    };
    const createRequire = jest.fn(() => loadSdk);
    const source = readFileSync(probe, 'utf8').split("<<'NODE'\n")[1].split('\nNODE\n')[0];
    await runInNewContext(source, {
      require: (name: string) => {
        if (name !== 'node:module') throw new Error('No SDK modules in the sandbox working directory');
        return { createRequire };
      },
      console: { log: (...values: string[]) => reports.push(values.join(' ')) },
      process: { env: {} }, Buffer, AbortSignal: { timeout: () => ({}) }, setTimeout: () => 0,
    });
    expect(createRequire).toHaveBeenCalledWith('/app/chat-sandbox-probe');
    expect(loaded).toEqual(clients);
    expect(reports).toEqual(['sdk_provider pass', 'sdk_sts pass', 'sdk_s3 pass', 'sdk_dynamodb pass', 'sdk_secrets pass', 'sdk_bedrock pass']);
  });

  it.each(['', 'sandbox:latest', '--privileged', `sha256:${'a'.repeat(63)}`])('rejects mutable or malformed image %p before Docker', reference => {
    const result = invoke(reference);
    expect(result.status).toBe(3);
    expect(result.stderr).toContain('immutable');
    expect(result.stdout).toBe('');
  });

  it('reports a missing runtime as blocked, not a passing probe', () => {
    rmSync(join(directory, 'docker'));
    const result = invoke();
    expect(result.status).toBe(3);
    expect(result.stderr).toContain('authorized Docker runtime');
    expect(result.stdout).toBe('');
  });

  it('does not pull or run an unavailable image', () => {
    const result = invoke(image, { DOCKER_FIXTURE_INSPECT_STATUS: '1' });
    expect(result.status).toBe(3);
    expect(readFileSync(log, 'utf8').split('\0').filter(Boolean)).toEqual(['image', 'inspect', image]);
  });

  it.each([image, `registry.example.test/chat@${image}`])('binds the smoke invocation to %s with no network or host mounts', reference => {
    const result = invoke(reference);
    expect(result.status).toBe(0);
    const argumentsList = readFileSync(log, 'utf8').split('\0').filter(Boolean);
    expect(argumentsList.slice(0, -1)).toEqual([
      'image', 'inspect', reference,
      'run', '--rm', '--pull', 'never', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
      '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
      '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m', '--workdir', '/tmp', '--entrypoint', '/bin/sh', reference, '-eu', '-c',
    ]);
    expect(argumentsList.at(-1)).toContain('test "$result" -eq 3');
    expect(argumentsList.at(-1)).toContain('runtime isolation was not tested');
    expect(argumentsList.at(-1)).toContain('test ! -e /var/run/adp-model/token');
    expect(result.stdout).toContain(`Verified packaging image: ${reference}`);
  });

  it('propagates failed image checks without printing verified packaging', () => {
    const result = invoke(image, { DOCKER_FIXTURE_RUN_STATUS: '19' });
    expect(result.status).toBe(19);
    expect(result.stdout).not.toContain('Verified packaging');
  });
});
