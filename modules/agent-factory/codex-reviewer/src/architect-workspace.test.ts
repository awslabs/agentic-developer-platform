import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { runDeveloper } from './developer.js';

for (const hasDesign of [true, false]) test(`native architect verifies shell workspace and design delivery (${hasDesign})`, { timeout: 60000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'architect-workspace-'));
  const workspace = join(root, 'repo'), bin = join(root, 'bin'), home = join(root, 'home');
  const before = { ...process.env };
  let requests = 0, sawArchitectRules = false, sawShell = false;
  const server = createServer((req, res) => {
    if (req.method !== 'POST') { res.writeHead(426).end(); return; }
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => {
      const body = JSON.parse(raw);
      sawArchitectRules ||= JSON.stringify(body).includes('personas/architect.md');
      const tool = body.tools.find((t: {name: string}) => ['shell_command', 'exec_command', 'shell'].includes(t.name));
      sawShell ||= !!tool;
      const command = 'git ls-files && mkdir -p docs && printf "# Release design\\n\\nInventory: README.md\\n" > docs/design.md && git add docs/design.md && git -c user.name=Fixture -c user.email=fixture@example.test commit -m "Document architecture"' + (hasDesign ? '' : ' && git rm docs/design.md && git -c user.name=Fixture -c user.email=fixture@example.test commit -m "Remove document"');
      const item = requests++ === 0 ? { type: 'function_call', id: 'fc_1', call_id: 'call_1', name: tool.name,
        arguments: JSON.stringify(tool.name === 'shell' ? {command: ['bash', '-c', command]} : {command, cmd: command, yield_time_ms: 10000}) }
        : { type: 'message', id: 'msg_1', role: 'assistant', content: [{ type: 'output_text', text: 'Design published: https://github.com/fixture/repo/pull/1; docs/design.md.' }] };
      res.writeHead(200, {'content-type': 'text/event-stream'});
      for (const [type, data] of [
        ['response.created', {response: {id: `response_${requests}`, status: 'in_progress'}}],
        ['response.output_item.done', {output_index: 0, item}],
        ['response.completed', {response: {id: `response_${requests}`, status: 'completed', output: [item], usage: {input_tokens: 10, output_tokens: 10, total_tokens: 20}}}],
      ] as const) res.write(`event: ${type}\ndata: ${JSON.stringify({type, ...data})}\n\n`);
      res.end();
    });
  });
  try {
    await Promise.all([mkdir(workspace), mkdir(bin), mkdir(home)]);
    const git = (...args: string[]) => execFileSync('git', args, {cwd: workspace, stdio: 'pipe'}).toString().trim();
    git('init'); await writeFile(join(workspace, 'README.md'), '# Fixture\n'); git('add', 'README.md');
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Fixture');
    git('switch', '-c', 'agent/issue-42');
    await writeFile(join(bin, 'gh'), `#!/bin/sh
case "$1 $2" in
  'issue view') echo '{"title":"Audit release components","comments":[]}' ;;
  'pr list') printf '[{"url":"https://github.com/fixture/repo/pull/1","headRefOid":"%s","isDraft":false,"changedFiles":1}]' "$(git rev-parse HEAD)" ;;
  'pr view') echo '{"files":[{"path":"docs/design.md"}]}' ;;
  *) exit 2 ;;
esac
`, {mode: 0o700});
    await writeFile(join(home, 'config.toml'), '[features]\nplugins = false\nrecommended_plugins = false\n');
    process.env.PATH = bin + ':' + before.PATH; process.env.CODEX_HOME = home;
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    const address = server.address(); assert.ok(address && typeof address !== 'string');
    const pending = runDeveloper({persona: 'architect', repository: 'fixture/repo', issue: 42, workspace,
      model: 'gpt-5-codex', baseUrl: `http://127.0.0.1:${address.port}/v1`, apiKey: 'fixture', timeoutMs: 45000}, true);
    if (hasDesign) {
      const result = await pending;
      assert.equal(result.prUrl, 'https://github.com/fixture/repo/pull/1');
      assert.match(await readFile(join(workspace, 'docs/design.md'), 'utf8'), /Release design/);
    } else {
      await assert.rejects(pending, /Markdown design document/);
    }
    assert.ok(sawArchitectRules); assert.ok(sawShell); assert.ok(requests >= 2);
    assert.equal(git('status', '--porcelain'), '');
  } finally {
    process.env = before; server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
    await rm(root, {recursive: true, force: true});
  }
});
