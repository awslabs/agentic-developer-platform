/** Command-time token renewal for the existing trusted ARC execution model.
 * Removes accidental signing-key inheritance; not same-UID OS isolation.
 */
import { spawn, execFileSync } from 'child_process';
import { copyFileSync, chmodSync, mkdirSync, realpathSync } from 'fs';
import { dirname, isAbsolute, join, resolve, sep } from 'path';
import type { SpawnOptions, SpawnedProcess } from '@anthropic-ai/claude-agent-sdk';
import { isMediatedRun } from './mediated-github-config';
import { canInitTokenManager, getToken, writeTokenFile } from './token-refresh';

export function captureRuntimeAppAuth(env: NodeJS.ProcessEnv = process.env) {
  const snapshot = { ...env };
  delete env.GH_APP_PRIVATE_KEY;
  delete env.GH_APP_KEY;
  const required = snapshot.ADP_REQUIRE_RENEWABLE_GITHUB === 'true';
  const mediated = isMediatedRun(snapshot);
  const pat = snapshot.ADP_TOKEN_MODE === 'pat';
  if (required && !mediated && !pat && (
    !canInitTokenManager(snapshot) || !/^[1-9]\d*$/.test(snapshot.GH_APP_INSTALLATION_ID || '') ||
    !/^[A-Za-z0-9_.-]+$/.test(snapshot.REPO_OWNER || '') ||
    !/^[A-Za-z0-9_.-]+$/.test(snapshot.REPO_NAME || '')
  )) throw new Error('Required renewable GitHub configuration is missing or invalid');
  return { required, enabled: canInitTokenManager(snapshot),
    privateKey: mediated || pat ? undefined : snapshot.GH_APP_PRIVATE_KEY || snapshot.GH_APP_KEY };
}

/** Publication errors propagate to the required workflow startup guard. */
export async function initializeRuntimeGitHubToken(): Promise<void> {
  const initialToken = await getToken();
  writeTokenFile(initialToken);
}

export function sanitizedSdkEnv(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const result = { ...env };
  delete result.GH_APP_PRIVATE_KEY;
  delete result.GH_APP_KEY;
  return result;
}

/** SDK's final process boundary, after model-policy/options environment merges. */
export function spawnSdkWithoutAppKey(options: SpawnOptions): SpawnedProcess {
  return spawn(options.command, options.args, {
    cwd: options.cwd, env: sanitizedSdkEnv(options.env), signal: options.signal,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
}

export function configureRuntimeGitHubAdapters(workDir: string, env: NodeJS.ProcessEnv = process.env): void {
  if (isMediatedRun(env) || env.ADP_TOKEN_MODE === 'pat') return;
  if (env.ADP_REQUIRE_RENEWABLE_GITHUB !== 'true') return;
  const tokenPath = env.ADP_TOKEN_FILE || '';
  if (!isAbsolute(tokenPath)) throw new Error('Renewable GitHub token path must be absolute');
  const workspace = realpathSync(workDir);
  const source = realpathSync(env.ADP_AGENT_SOURCE_DIR || resolve(__dirname, '../../../..'));
  const within = (candidate: string, root: string) => candidate === root || candidate.startsWith(root + sep);
  if (within(resolve(tokenPath), workspace) || within(resolve(tokenPath), source)) {
    throw new Error('Renewable GitHub token path must be outside the workspace');
  }
  const authDir = dirname(tokenPath);
  mkdirSync(authDir, { recursive: true, mode: 0o700 });
  const resolvedDir = realpathSync(authDir);
  if (within(resolvedDir, workspace) || within(resolvedDir, source)) throw new Error('GitHub auth directory resolves inside a workspace');
  chmodSync(authDir, 0o700);
  const bin = join(authDir, 'bin');
  mkdirSync(bin, { mode: 0o700, recursive: true });
  const realGh = realpathSync(execFileSync('which', ['gh'], { env, encoding: 'utf8' }).trim());
  for (const file of ['gh', 'git-credential']) {
    const target = join(bin, file);
    copyFileSync(resolve(__dirname, '../scripts/github-auth', file), target);
    chmodSync(target, 0o700);
  }
  if (realGh === realpathSync(join(bin, 'gh'))) throw new Error('Recursive GitHub wrapper configuration');
  env.ADP_REAL_GH = realGh;
  env.PATH = `${bin}${sep === '/' ? ':' : ';'}${env.PATH || ''}`;
  // Clear inherited helpers and HTTP extraheaders; no global config mutation.
  // A repository-specific helper answers only HTTPS github.com/owner/repo.
  env.GIT_CONFIG_COUNT = '5';
  env.GIT_CONFIG_KEY_0 = 'credential.helper'; env.GIT_CONFIG_VALUE_0 = '';
  env.GIT_CONFIG_KEY_1 = 'credential.https://github.com.helper';
  env.GIT_CONFIG_VALUE_1 = `!'${join(bin, 'git-credential').replace(/'/g, "'\\''")}'`;
  env.GIT_CONFIG_KEY_2 = 'credential.useHttpPath'; env.GIT_CONFIG_VALUE_2 = 'true';
  env.GIT_CONFIG_KEY_3 = 'http.extraheader'; env.GIT_CONFIG_VALUE_3 = '';
  env.GIT_CONFIG_KEY_4 = 'http.https://github.com/.extraheader'; env.GIT_CONFIG_VALUE_4 = '';
  env.GIT_TERMINAL_PROMPT = '0';
  delete env.GIT_CONFIG_PARAMETERS;
  env.GIT_ASKPASS = '/bin/false';
  env.SSH_ASKPASS = '/bin/false';
}
