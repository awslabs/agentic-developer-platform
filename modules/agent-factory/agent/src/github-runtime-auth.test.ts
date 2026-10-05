import { mkdtempSync, writeFileSync, rmSync, mkdirSync, renameSync, symlinkSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve } from 'path';
import { spawn, spawnSync } from 'child_process';
import { once } from 'events';
import { createInterface } from 'readline';

const mockInstallationAuth = jest.fn();
jest.mock('@octokit/auth-app', () => ({ createAppAuth: jest.fn(() => mockInstallationAuth) }));

const originalEnv = { ...process.env };
let dir: string;
let env: NodeJS.ProcessEnv;
const scripts = resolve(__dirname, '../scripts/github-auth');
const appEnv = () => ({
  GH_APP_ID: '123', GH_APP_PRIVATE_KEY: 'test-only-signing-material', GH_APP_KEY: 'test-only-alias',
  GH_APP_INSTALLATION_ID: '456', REPO_OWNER: 'owner', REPO_NAME: 'repo', ADP_REQUIRE_RENEWABLE_GITHUB: 'true',
});

beforeEach(() => {
  jest.resetModules();
  dir = mkdtempSync(join(tmpdir(), 'github-auth-test-'));
  env = { PATH: process.env.PATH, ...appEnv(), ADP_TOKEN_FILE: join(dir, 'auth/token') };
  process.env = { ...originalEnv, ...env, ADP_TOKEN_MODE: '', ADP_MEDIATED_GITHUB_ENABLED: '', ADP_AGENT_AUTHORITY_ENABLED: '', ADP_GH_TOKEN_BROKER_ENABLED: '', GITHUB_TOKEN_BROKER_URL: '' };
  mkdirSync(join(dir, 'workspace'));
  mkdirSync(join(dir, 'auth'));
  writeFileSync(env.ADP_TOKEN_FILE!, 'current-token', { mode: 0o600 });
  const fakeGh = join(dir, 'real-gh');
  writeFileSync(fakeGh, '#!/usr/bin/env node\nprocess.stdout.write(JSON.stringify({args:process.argv.slice(2), token:process.env.GH_TOKEN, keyPresent:!!(process.env.GH_APP_PRIVATE_KEY||process.env.GH_APP_KEY)}))', { mode: 0o700 });
  env.ADP_REAL_GH = fakeGh;
  mockInstallationAuth.mockReset();
});
afterEach(() => { jest.restoreAllMocks(); process.env = originalEnv; rmSync(dir, { recursive: true, force: true }); });

function gh(args: string[], extra = {}) { return spawnSync(join(scripts, 'gh'), args, { env: { ...env, ...extra }, encoding: 'utf8' }); }
function credential(input = 'protocol=https\nhost=github.com\npath=owner/repo.git\n\n', operation = 'get', extra = {}) {
  return spawnSync(join(scripts, 'git-credential'), [operation], { input, env: { ...env, ...extra }, encoding: 'utf8' });
}

it('reads the current token for each command, preserves argv and filters signing aliases', () => {
  const args = ['api', '-X', 'POST', '/repos/owner/repo/issues', '-f', 'body=https://example.org/a $value `literal`'];
  const first = gh(args, { GH_TOKEN: 'expired' });
  expect(first.status).toBe(0);
  expect(JSON.parse(first.stdout)).toEqual({ args, token: 'current-token', keyPresent: false });
  writeFileSync(join(dir, 'replacement'), 'renewed-token');
  renameSync(join(dir, 'replacement'), env.ADP_TOKEN_FILE!);
  expect(JSON.parse(gh(args).stdout).token).toBe('renewed-token');
  expect(credential().stdout).toBe('username=x-access-token\npassword=renewed-token\n\n');
});
it.each([
  ['api', 'https://foreign.test/repos/owner/repo'], ['api', '--hostname', 'foreign.test', '/user'],
  ['api', '--hostname=foreign.test', '/user'], ['api', 'http://api.github.com/user'],
  ['api', 'https://user:pass@api.github.com/user'],
])('refuses foreign gh destinations %j', (...args) => { expect(gh(args).status).not.toBe(0); });
it('fails closed on missing tokens and recursion', () => {
  rmSync(env.ADP_TOKEN_FILE!);
  expect(gh(['api', '/user']).status).not.toBe(0);
  expect(credential().status).not.toBe(0);
  writeFileSync(env.ADP_TOKEN_FILE!, 'token');
  expect(gh(['api', '/user'], { ADP_REAL_GH: join(scripts, 'gh') }).status).not.toBe(0);
});
it.each(['protocol=http\nhost=github.com\npath=owner/repo', 'protocol=https\nhost=foreign.test\npath=owner/repo', 'protocol=https\nhost=github.com\npath=other/repo'])('refuses wrong credential targets %s', input => {
  expect(credential(input).stdout).toBe('');
  expect(credential(input).status).not.toBe(0);
});
it('ignores store/erase and uses only the fresh finalization token', () => {
  expect(credential('', 'store').stdout).toBe('');
  expect(credential('', 'erase').stdout).toBe('');
  expect(credential(undefined, 'get', { ADP_GITHUB_FINALIZATION: 'true', GH_TOKEN: 'final-token' }).stdout).toContain('password=final-token');
  expect(credential(undefined, 'get', { ADP_GITHUB_FINALIZATION: 'true', GH_TOKEN: '' }).status).not.toBe(0);
});
it.each([{ ADP_TOKEN_MODE: 'mediated' }, ...['1', 'true', 'yes', 'TRUE', 'YES'].map(value => ({ ADP_MEDIATED_GITHUB_ENABLED: value }))])('never selects direct auth during mediation %j', flags => {
  const { captureRuntimeAppAuth, configureRuntimeGitHubAdapters } = require('./github-runtime-auth');
  const mediated: NodeJS.ProcessEnv = { ...env, ...flags };
  expect(captureRuntimeAppAuth(mediated).enabled).toBe(false);
  expect(mediated.GH_APP_PRIVATE_KEY).toBeUndefined();
  expect(mediated.GH_APP_KEY).toBeUndefined();
  configureRuntimeGitHubAdapters('/does/not/exist', mediated);
  expect(mediated.PATH).toBe(env.PATH);
  expect(gh(['api', '/user'], flags).status).not.toBe(0);
  expect(credential(undefined, 'get', flags).status).not.toBe(0);
  expect(mockInstallationAuth).not.toHaveBeenCalled();
});
it('preserves PAT mode without selecting adapters or minting', () => {
  const { captureRuntimeAppAuth, configureRuntimeGitHubAdapters } = require('./github-runtime-auth');
  const pat: NodeJS.ProcessEnv = { ...env, ADP_TOKEN_MODE: 'pat', GH_TOKEN: 'pat-token' };
  expect(captureRuntimeAppAuth(pat).enabled).toBe(false);
  configureRuntimeGitHubAdapters('/missing', pat);
  expect(pat.GH_TOKEN).toBe('pat-token');
  expect(pat.PATH).toBe(env.PATH);
  expect(mockInstallationAuth).not.toHaveBeenCalled();
});
it.each([{ GH_APP_INSTALLATION_ID: '' }, { GH_APP_INSTALLATION_ID: '0' }, { GH_APP_INSTALLATION_ID: 'wrong' }, { REPO_NAME: '' }, { REPO_OWNER: '../foreign' }, { GH_APP_ID: '' }])('rejects required invalid configuration %j', change => {
  const { captureRuntimeAppAuth } = require('./github-runtime-auth');
  expect(() => captureRuntimeAppAuth({ ...env, ...change })).toThrow('configuration');
});
it('configures real Git to use the helper and clears old checkout extraheaders', () => {
  const { configureRuntimeGitHubAdapters } = require('./github-runtime-auth');
  const cwd = join(dir, 'workspace');
  spawnSync('git', ['init', '--quiet', cwd]);
  spawnSync('git', ['config', 'credential.helper', '!echo username=stale; echo password=stale'], { cwd });
  configureRuntimeGitHubAdapters(cwd, env);
  const result = spawnSync('git', ['credential', 'fill'], { cwd, env, encoding: 'utf8', input: 'protocol=https\nhost=github.com\npath=owner/repo.git\n\n' });
  expect(result.status).toBe(0);
  expect(result.stdout).toContain('password=current-token');
  const headers = spawnSync('git', ['config', '--get-all', 'http.https://github.com/.extraheader'], { env, encoding: 'utf8' });
  expect(headers.stdout).toBe('\n');
  expect(env.GIT_ASKPASS).toBe('/bin/false');
  // Exercise the actual workflow fallback helper syntax with a stopped/expired file.
  const final = spawnSync('git', ['-c', 'credential.helper=', '-c', `credential.https://github.com.helper=!'${join(scripts, 'git-credential')}'`, '-c', 'credential.useHttpPath=true', 'credential', 'fill'], {
    cwd, env: { PATH: env.PATH, REPO_OWNER: 'owner', REPO_NAME: 'repo', GH_TOKEN: 'fresh-finalization', ADP_GITHUB_FINALIZATION: 'true', GIT_TERMINAL_PROMPT: '0' },
    encoding: 'utf8', input: 'protocol=https\nhost=github.com\npath=owner/repo.git\n\n',
  });
  expect(final.status).toBe(0);
  expect(final.stdout).toContain('password=fresh-finalization');
});
it('refuses token paths inside either workspace, including directory symlinks', () => {
  const { configureRuntimeGitHubAdapters } = require('./github-runtime-auth');
  for (const path of [join(dir, 'workspace'), join(dir, 'workspace/token'), resolve(__dirname, '../token')]) {
    expect(() => configureRuntimeGitHubAdapters(join(dir, 'workspace'), { ...env, ADP_TOKEN_FILE: path })).toThrow();
  }
  symlinkSync(join(dir, 'workspace'), join(dir, 'linked-workspace'));
  expect(() => configureRuntimeGitHubAdapters(join(dir, 'workspace'), { ...env, ADP_TOKEN_FILE: join(dir, 'linked-workspace/token') })).toThrow();
});
it('filters the final SDK spawn environment on initial, retry and resumed invocations', async () => {
  const { captureRuntimeAppAuth, spawnSdkWithoutAppKey } = require('./github-runtime-auth');
  const captured = captureRuntimeAppAuth();
  expect(Boolean(captured.privateKey)).toBe(true);
  expect(process.env.GH_APP_PRIVATE_KEY).toBeUndefined();
  for (const run of ['initial', 'retry', 'resume']) {
    const child = spawnSdkWithoutAppKey({ command: process.execPath, args: ['-e', 'process.stdout.write(JSON.stringify({safe:!(process.env.GH_APP_PRIVATE_KEY||process.env.GH_APP_KEY),run:process.env.RUN,cwd:process.cwd()}))'], cwd: dir, env: { ...process.env, ...appEnv(), RUN: run }, signal: new AbortController().signal });
    let output = ''; child.stdout.on('data', (data: Buffer) => { output += data; });
    expect((await once(child, 'close'))[0]).toBe(0);
    expect(JSON.parse(output)).toEqual({ safe: true, run, cwd: dir });
  }
});
it('renews for three hours while one existing child repeatedly invokes gh and git', async () => {
  const { captureRuntimeAppAuth, initializeRuntimeGitHubToken } = require('./github-runtime-auth');
  const manager = require('./token-refresh');
  let now = Date.now(); let minted = 0;
  jest.spyOn(Date, 'now').mockImplementation(() => now);
  mockInstallationAuth.mockImplementation(async () => ({ token: `mint-${++minted}`, expiresAt: new Date(now + 60 * 60_000).toISOString() }));
  const captured = captureRuntimeAppAuth();
  manager.initTokenManager({ appId: '123', privateKey: captured.privateKey, installationId: '456', owner: 'owner', repo: 'repo', refreshThresholdMs: 20 * 60_000 });
  // Unknown bootstrap expiry must cause a new mint, rather than inventing a TTL.
  manager.adoptBootstrapToken({ GH_TOKEN: 'expired-bootstrap' });
  await initializeRuntimeGitHubToken();
  const child = spawn(process.execPath, ['-e', `const {spawnSync}=require('child_process'); require('readline').createInterface({input:process.stdin}).on('line',()=>{const gh=spawnSync(${JSON.stringify(join(scripts, 'gh'))},['api','/repos/owner/repo'],{encoding:'utf8'});const git=spawnSync(${JSON.stringify(join(scripts, 'git-credential'))},['get'],{encoding:'utf8',input:'protocol=https\\nhost=github.com\\npath=owner/repo.git\\n\\n'});process.stdout.write(JSON.stringify({gh:JSON.parse(gh.stdout).token,git:git.stdout})+'\\n');});`], { env: { ...env, GH_TOKEN: 'expired-bootstrap' }, stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout });
  try {
    for (let minute = 0; minute <= 180; minute += 5) {
      if (minute) now += 5 * 60_000;
      const token = await manager.getToken();
      expect(manager.getTokenStatus().valid).toBe(true);
      const next = once(lines, 'line'); child.stdin.write('next\n');
      const result = JSON.parse((await next)[0]);
      expect(result.gh).toBe(token);
      expect(result.git).toContain(`password=${token}\n`);
    }
    expect(minted).toBeGreaterThanOrEqual(4);
    expect(mockInstallationAuth).toHaveBeenCalledWith({ type: 'installation', repositoryNames: ['repo'] });
    expect(process.env.GH_APP_PRIVATE_KEY).toBeUndefined();
  } finally { lines.close(); child.stdin.end(); await once(child, 'close'); }
}, 20000);
it('propagates startup mint and publication failures and never reports false refresh success', async () => {
  const { initializeRuntimeGitHubToken } = require('./github-runtime-auth');
  const manager = require('./token-refresh');
  manager.initTokenManager({ appId: '123', privateKey: 'test-only', installationId: '456', owner: 'owner', repo: 'repo' });
  mockInstallationAuth.mockRejectedValue(new Error('mint refused'));
  await expect(initializeRuntimeGitHubToken()).rejects.toThrow('mint refused');
  expect(manager.getTokenStatus()).toBeNull();
  mockInstallationAuth.mockResolvedValue({ token: 'fresh', expiresAt: new Date(Date.now() + 3600000).toISOString() });
  rmSync(env.ADP_TOKEN_FILE!); mkdirSync(env.ADP_TOKEN_FILE!);
  await expect(initializeRuntimeGitHubToken()).rejects.toThrow();
  expect(manager.getTokenStatus()).toBeNull();
});
