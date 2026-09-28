import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { buildFallbackGateComment, GATE_APPROVAL_EFFECTS, ensureGateComment, commitDirtyAidlcState } from './aidlc-gate-enforcer';
import { execSync } from 'child_process';
jest.mock('child_process', () => ({ execSync: jest.fn() }));
const git = execSync as jest.Mock;

it.each(Object.keys(GATE_APPROVAL_EFFECTS))('fallback for %s explains the decision with usable reply syntax', stage => {
  const body = buildFallbackGateComment(stage);
  expect(body.startsWith(`<!-- aidlc-gate:${stage} -->`)).toBe(true);
  expect(body).toContain('`@agent-aidlc approve`');
  expect(body).toContain(GATE_APPROVAL_EFFECTS[stage]);
  expect(body).toContain('Artifact publication has not been verified');
  expect(body).not.toContain('artifacts have been committed');
  if (stage === 'loop-proposal') {
    expect(body).not.toContain('`@agent-aidlc skip`');
    expect(body).toContain('does not authorize construction');
  }
});

it.each([true, false])('links only a confirmed remote artifact revision (published=%s)', async published => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-gate-'));
  const statePath = 'aidlc/spaces/issue-42/aidlc-state.md';
  fs.mkdirSync(path.dirname(path.join(cwd, statePath)), { recursive: true });
  fs.writeFileSync(path.join(cwd, statePath), '**Stage**: Loop Proposal\n**Waiting For**: Human input');
  const sha = 'a'.repeat(40);
  git.mockImplementation((command: string) => command === 'git rev-parse HEAD' ? sha
    : command.startsWith('git ls-tree') ? statePath : '');
  const deps = {
    cwd, issueNumber: '42', repoOwner: 'org', repoName: 'repo', log: jest.fn(),
    execCommand: jest.fn(async (command: string) => command.startsWith('gh api') && published ? sha : ''),
    postComment: jest.fn(async () => {}),
  };
  try {
    await ensureGateComment(deps);
    const body = (deps.postComment.mock.calls as unknown as string[][])[0][0];
    if (published) expect(body).toContain(`https://github.com/org/repo/tree/${sha}/aidlc/spaces/issue-42`);
    else expect(body).toContain('Artifact publication has not been verified');
  } finally { fs.rmSync(cwd, { recursive: true, force: true }); }
});

it('does not report a failed git push as publication when the error carries stdout', async () => {
  git.mockReset().mockReturnValueOnce(' M aidlc/state.md').mockReturnValueOnce('').mockReturnValueOnce('')
    .mockImplementationOnce(() => { throw Object.assign(new Error('push rejected'), { stdout: '' }); });
  expect(await commitDirtyAidlcState({ cwd: '/tmp', issueNumber: '42', repoOwner: 'o', repoName: 'r',
    log: jest.fn(), execCommand: jest.fn(), postComment: jest.fn() })).toBe(false);
});

it('does not post a different issue’s pending gate', async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-gate-'));
  const dir = path.join(cwd, 'aidlc/spaces/issue-99');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'aidlc-state.md'), '**Stage**: Loop Proposal\n**Waiting For**: Human input');
  const postComment = jest.fn();
  try {
    await ensureGateComment({ cwd, issueNumber: '42', repoOwner: 'o', repoName: 'r',
      log: jest.fn(), execCommand: jest.fn(), postComment });
    expect(postComment).not.toHaveBeenCalled();
  } finally { fs.rmSync(cwd, { recursive: true, force: true }); }
});

// Execute the actual workflow script against a fake GitHub API; no external writes.
it.each(['requirements-analysis', 'loop-proposal', 'unknown-stage'])('reminder for %s links its gate and preserves approval scope', async stage => {
  const workflow = fs.readFileSync(path.resolve(__dirname, '../../../../.github/workflows/aidlc-gate-nudge.yml'), 'utf8');
  const script = workflow.split('          script: |\n')[1].split('\n').map(line => line.slice(12)).join('\n');
  const gateUrl = 'https://github.com/org/repo/issues/42#issuecomment-10';
  const createComment = jest.fn(async () => {});
  const listForRepo = jest.fn(); const listComments = jest.fn();
  const github = {
    rest: { issues: { listForRepo, listComments, createComment, getLabel: jest.fn(), addLabels: jest.fn() } },
    paginate: jest.fn(async method => method === listForRepo ? [{ number: 42, labels: [] }] : [{
      body: `<!-- aidlc-gate:${stage} -->`, html_url: gateUrl,
      created_at: new Date(Date.now() - 5 * 86400000).toISOString(), user: { login: 'agent[bot]' },
    }]),
  };
  const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
  await new AsyncFunction('github', 'core', 'context', script)(github, { info: jest.fn() }, { eventName: 'schedule', repo: { owner: 'org', repo: 'repo' } });
  const body = (createComment.mock.calls as unknown as { body: string }[][])[0][0].body;
  expect(body).toContain('<!-- aidlc-nudge -->');
  expect(body).toContain(gateUrl);
  expect(body).toContain('`@agent-aidlc feedback: [your notes]`');
  if (stage === 'unknown-stage') expect(body).not.toContain('`@agent-aidlc approve`');
  else expect(body).toContain(GATE_APPROVAL_EFFECTS[stage]);
  if (stage === 'loop-proposal') expect(body).not.toContain('`@agent-aidlc skip`');
});
