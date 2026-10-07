/** Ordinary transport-contract regressions; no HTTP server or external requests. */
import { GitLabClient } from '../clients/gitlab_client';
import { VaultGatewayClient } from '../complex-task-chat/vault/gateway-client';
import { LiveStatusComment } from '../github-comments';
import { CheckRunStreamer } from '../components/checkRunStreamer';

const mockFetch = jest.fn();
const gitlab = () => new GitLabClient({ baseUrl: 'https://gitlab.example.com', accessToken: 'fixture-gitlab-token' });
const vault = () => new VaultGatewayClient({ baseUrl: 'https://gateway.example.com', apiKey: 'fixture-vault-key' });
const identity = { user_id: 'fixture-user', agent_id: 'fixture-agent', task_id: 'fixture-task', service: 'aws' };

const originalGitLabUrl = process.env.GITLAB_URL;
const originalVaultUrl = process.env.VAULT_GATEWAY_URL;
beforeEach(() => {
  process.env.GITLAB_URL = 'https://gitlab.example.com';
  process.env.VAULT_GATEWAY_URL = 'https://gateway.example.com';
  mockFetch.mockReset(); global.fetch = mockFetch as typeof fetch;
});
afterEach(() => {
  if (originalGitLabUrl === undefined) delete process.env.GITLAB_URL;
  else process.env.GITLAB_URL = originalGitLabUrl;
  if (originalVaultUrl === undefined) delete process.env.VAULT_GATEWAY_URL;
  else process.env.VAULT_GATEWAY_URL = originalVaultUrl;
});

test('vault refuses missing operator configuration before sending the API key', () => {
  delete process.env.VAULT_GATEWAY_URL;
  expect(vault).toThrow('VAULT_GATEWAY_URL must configure');
  expect(mockFetch).not.toHaveBeenCalled();
});

test.each([
  'https://attacker.example',
  'https://gateway.example.com.attacker.example',
  'https://gateway.example.com:8443',
  'http://gateway.example.com',
  'https://gateway.example.com@attacker.example',
  'https://gateway.example.com/api',
  'https://gateway.example.com?next=attacker',
  'https://169.254.169.254',
])('vault refuses an untrusted caller destination before sending the API key: %s', baseUrl => {
  expect(() => new VaultGatewayClient({ baseUrl, apiKey: 'fixture-vault-key' })).toThrow();
  expect(mockFetch).not.toHaveBeenCalled();
});

test('vault retains operator-configured internal HTTP and encodes query identifiers', async () => {
  process.env.VAULT_GATEWAY_URL = 'http://gateway.internal:8080/';
  mockFetch.mockResolvedValue({ ok: true, json: async () => [] });
  const client = new VaultGatewayClient({ baseUrl: 'http://gateway.internal:8080', apiKey: 'fixture-vault-key' });
  await expect(client.listCredentials('user/../other?x=y', 'run&foreign')).resolves.toEqual([]);
  expect(mockFetch).toHaveBeenCalledWith(
    'http://gateway.internal:8080/internal/v1/user-credentials?user_id=user%2F..%2Fother%3Fx%3Dy&invocation_id=run%26foreign',
    expect.objectContaining({ redirect: 'error', headers: expect.objectContaining({ 'X-Internal-Api-Key': 'fixture-vault-key' }) }),
  );
});

const calls: Array<[string, () => Promise<unknown>]> = [
  ['GitLab issue note (2022)', () => gitlab().postIssueComment(1, 2, 'private note')],
  ['GitLab branch (2023)', () => gitlab().createBranch(1, 'feature', 'main')],
  ['GitLab merge request (2024)', () => gitlab().createMergeRequest(1, { sourceBranch: 'feature', targetBranch: 'main', title: 'private title' })],
  ['GitLab file (2025)', () => gitlab().getFile(1, 'private.txt', 'main')],
  ['Vault list (2078)', () => vault().listCredentials('fixture-user', 'fixture-run')],
  ['Vault POST (2079)', () => vault().assumeRole(identity)],
];

test.each(calls)('%s refuses transport redirects and propagates failure', async (_name, invoke) => {
  // Node fetch rejects a redirect when redirect:error is set. The known runtime
  // behavior is already recorded by S04; pin each caller's transport contract.
  mockFetch.mockRejectedValue(new TypeError('redirect refused'));
  await expect(invoke()).rejects.toThrow('redirect refused');
  expect(mockFetch).toHaveBeenCalledTimes(1);
  const [, options] = mockFetch.mock.calls[0];
  expect(options.redirect).toBe('error');
  expect(options.headers['PRIVATE-TOKEN'] || options.headers['X-Internal-Api-Key']).toBeTruthy();
});

test.each(calls)('%s still processes direct destination responses', async (_name, invoke) => {
  mockFetch.mockResolvedValue({ ok: true, json: async () => ({ iid: 12, web_url: 'https://gitlab.example.com/mr/12', content: 'b2s=' }) });
  await invoke();
  expect(mockFetch).toHaveBeenCalledTimes(1);
  expect(mockFetch.mock.calls[0][1].redirect).toBe('error');
});

test('GitHub status comment does not record a redirected response as a posted comment (2157)', async () => {
  const comment = new LiveStatusComment([], { owner: 'fixture', repo: 'repo', issueNumber: 1, token: 'fixture-token' });
  mockFetch.mockRejectedValue(new TypeError('redirect refused'));
  try {
    await expect(comment.post()).rejects.toThrow('redirect refused');
    expect(comment.getCommentUrl()).toBeNull();
    expect(mockFetch.mock.calls[0][1].redirect).toBe('error');
  } finally {
    await comment.finalizeFailure({ error: 'fixture cleanup', durationMs: 0 });
  }
});

test('GitHub check-run redirect failure reaches the existing fail-soft error hook (2123)', async () => {
  const failed = jest.fn();
  const streamer = new CheckRunStreamer({ repo: 'fixture/repo', checkRunId: 1, tokenProvider: () => 'fixture-token', persona: 'developer', issueNumber: 1,
    model: 'fixture-model', onPatchError: failed, log: () => {} });
  mockFetch.mockRejectedValue(new TypeError('redirect refused'));
  streamer.onResult({});
  await new Promise(resolve => setTimeout(resolve, 0));
  streamer.destroy();
  expect(mockFetch.mock.calls[0][1].redirect).toBe('error');
  expect(failed).toHaveBeenCalledWith('redirect refused');
});
