/** Real SDK native file-tool execution against a local fixture model. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { Codex } from '@openai/codex-sdk';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, rm, readFile, cp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

function events(item: Record<string, any>, count: number): string {
  const output: string[] = [];
  const emit = (type: string, fields: object) => output.push(`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`);
  const response = { id: `resp_${count}`, object: 'response', status: 'completed', output: [item],
    usage: { input_tokens: 100, output_tokens: 10, total_tokens: 110 } };
  emit('response.created', { response: { ...response, status: 'in_progress', output: [] } });
  emit('response.output_item.added', { output_index: 0, item: { ...item, status: 'in_progress', ...(item.type === 'function_call' ? { arguments: '' } : { content: [] }) } });
  if (item.type === 'function_call') {
    emit('response.function_call_arguments.delta', { output_index: 0, item_id: item.id, delta: item.arguments });
    emit('response.function_call_arguments.done', { output_index: 0, item_id: item.id, arguments: item.arguments });
  } else {
    emit('response.content_part.added', { item_id: item.id, output_index: 0, content_index: 0, part: { type: 'output_text', text: '', annotations: [] } });
    emit('response.output_text.delta', { item_id: item.id, output_index: 0, content_index: 0, delta: item.content[0].text });
    emit('response.output_text.done', { item_id: item.id, output_index: 0, content_index: 0, text: item.content[0].text });
    emit('response.content_part.done', { item_id: item.id, output_index: 0, content_index: 0, part: item.content[0] });
  }
  emit('response.output_item.done', { output_index: 0, item });
  emit('response.completed', { response });
  return output.join('');
}

for (const persona of ['developer', 'reviewer'] as const) test(`${persona}: real SDK reads an exact frozen installed skill`, async () => {
  const root = await mkdtemp(join(tmpdir(), 'native-skill-use-'));
  const home = join(root, 'home');
  const workspace = join(root, 'workspace');
  await mkdir(join(home, '.codex'), { recursive: true }); await mkdir(workspace);
  const installed = join(root, 'app');
  for (const name of ['codex-reviewer', 'codex-harness']) {
    await mkdir(join(installed, name, 'dist'), { recursive: true });
    await writeFile(join(installed, name, 'package.json'), '{"type":"module"}');
  }
  for (const name of ['shared-instructions.js', 'skill-catalog.js']) {
    await cp(new URL(name, import.meta.url), join(installed, 'codex-reviewer/dist', name));
  }
  await cp(new URL('../../codex-harness/dist/projection.js', import.meta.url), join(installed, 'codex-harness/dist/projection.js'));
  await cp(new URL('../../rules/', import.meta.url), join(installed, 'codex-harness/rules'), { recursive: true });
  await cp(new URL('../../skills/', import.meta.url), join(installed, 'skills'), { recursive: true });
  await cp(new URL('../../../domain-apps/superplane/agent/skills/', import.meta.url), join(installed, 'skills'), { recursive: true });
  const { loadSharedInstructions } = await import(pathToFileURL(join(installed, 'codex-reviewer/dist/shared-instructions.js')).href);
  const instructions = loadSharedInstructions(persona, 'Read the selected skill and report the observed evidence.');
  const path = instructions.text.match(/Read (\/[^\n;]+\/superplane\/SKILL.md); version sha256:/)?.[1];
  assert.ok(path);
  const expected = await readFile(path, 'utf8');
  let calls = 0;
  let observed = '';
  const server = createServer(async (request, response) => {
    if (request.method !== 'POST') { response.writeHead(404).end(); return; }
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    calls++;
    if (calls > 1) observed = body.input.filter((item: any) => item.type === 'function_call_output').map((item: any) => item.output).join('\n');
    const item = calls === 1 ? { id: 'fc_read', type: 'function_call', call_id: 'read_skill', name: 'exec_command', status: 'completed',
      arguments: JSON.stringify({ cmd: `cat '${path.replaceAll("'", "'\\''")}'`, login: false, max_output_tokens: 20000 }) }
      : { id: 'msg_done', type: 'message', role: 'assistant', status: 'completed', phase: 'final_answer',
        content: [{ type: 'output_text', text: 'Observed installed skill.', annotations: [] }] };
    response.writeHead(200, { 'content-type': 'text/event-stream' }).end(events(item, calls));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const address = server.address(); assert.ok(address && typeof address !== 'string');
    const sdk = new Codex({ baseUrl: `http://127.0.0.1:${address.port}/v1`, apiKey: 'local-fixture',
      config: { developer_instructions: instructions.text, features: { plugins: false, recommended_plugins: false } },
      env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: home, CODEX_HOME: join(home, '.codex'), TMPDIR: root } });
    // Native workers use their existing pod boundary. The fixture emits only
    // this exact read command and the child inherits no provider credentials.
    const thread = sdk.startThread({ workingDirectory: workspace, skipGitRepoCheck: true, model: 'gpt-5-codex',
      sandboxMode: 'danger-full-access', approvalPolicy: 'never', networkAccessEnabled: false });
    const result = await thread.run('Inspect the maintained Superplane integration guidance for this repository task. Read its frozen skill before proposing work.', { signal: AbortSignal.timeout(30000) });
    assert.equal(calls, 2);
    assert.equal(result.finalResponse, 'Observed installed skill.');
    assert.ok(observed.includes(expected.trim()), `Actual SDK tool result did not include the skill: ${observed.slice(0, 1000)}`);
    instructions.verify();
  } finally {
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
    await rm(root, { recursive: true, force: true });
  }
});
