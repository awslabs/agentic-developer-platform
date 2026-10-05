// Run only inside the exact built worker image, with --network none.
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { spawn, execFileSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { randomUUID, createHash } from 'node:crypto';

const root = '/fixture';
const fixturePath = `${root}/docs/task-api/contracts/v1/fixtures/valid/process-start-frame.json`;
const fixtureBytes = readFileSync(fixturePath);
assert.equal(createHash('sha256').update(fixtureBytes).digest('hex'), process.env.FIXTURE_HASH);
const start = JSON.parse(fixtureBytes);
delete start['$fixture'];
const entry = '/app/task-agents/investigator/dist/index.js';
for (const binary of [entry, '/app/dist/agent-worker.js', '/app/codex-reviewer/dist/index.js']) {
  assert.ok(existsSync(binary), `missing packaged binary: ${binary}`);
  execFileSync(process.execPath, ['--check', binary]);
}
assert.equal(existsSync('/app/task-agents/investigator/node_modules'), false);
// Evaluate just the actual installed selector's AST, without starting a worker.
execFileSync('python3', ['-c', `
import ast
from pathlib import Path
source = Path('/app/entrypoint.py').read_text()
tree = ast.parse(source)
names = {'AGENT_BINARY', 'CODEX_REVIEWER_BINARY', 'CODEX_PERSONA_PREFIX'}
selected = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in {'persona_runtime', 'worker_command'}) or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]
ns = {}
exec(compile(ast.Module(body=selected, type_ignores=[]), '<installed-selector>', 'exec'), ns)
assert ns['worker_command']('agent-developer') == ['node', '/app/dist/agent-worker.js']
assert ns['worker_command']('agent-codex-reviewer') == ['node', '/app/codex-reviewer/dist/index.js', '--embedded']
`]);
const report = JSON.stringify({ summary: 'Supplied instructions were examined.', findings: [{ statement: 'The investigation is limited to the supplied task instructions and evidence.', evidence_refs: ['instructions'], confidence: 'high' }], uncertainties: ['No additional evidence was available.'], recommendations: [] });
async function run(scenario) {
  const child = spawn(process.execPath, [entry, '--embedded'], {
    env: { PATH: '/usr/local/bin:/usr/bin:/bin', NODE_OPTIONS: `--require=${root}/modules/agent-factory/task-agents/investigator/test/network-deny-hook.cjs` },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const frames = [];
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.stdin.on('error', () => {});
  const send = frame => child.stdin.write(`${JSON.stringify(frame)}\n`);
  const timer = setTimeout(() => child.kill('SIGKILL'), 10000);
  let parseError;
  createInterface({ input: child.stdout }).on('line', line => {
    try {
      const frame = JSON.parse(line);
      frames.push(frame);
      if (frame.type === 'model.request') {
        if (scenario === 'cancelled') {
          send({ protocol_version: 1, type: 'cancel', request_id: randomUUID(), task_id: start.task_id, command_id: randomUUID(), intentional: true, reason: 'Image smoke cancellation' });
        } else {
          send({ protocol_version: 1, type: 'model.result', request_id: randomUUID(), task_id: start.task_id, turn_id: frame.turn_id, operation_status: 'confirmed', content: [{ type: 'text', text: scenario === 'malformed' ? 'not JSON' : report }] });
        }
      }
    } catch (error) { parseError = error; child.kill('SIGKILL'); }
  });
  const code = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', resolve);
    send({ ...start, limits: { ...start.limits, max_turns: 1 } });
  }).finally(() => clearTimeout(timer));
  if (parseError) throw parseError;
  assert.match(stderr, /network-deny observer active/);
  assert.doesNotMatch(stderr, /network request blocked:/);
  const terminal = scenario === 'useful' ? 'result' : scenario === 'cancelled' ? 'cancelled' : 'error';
  assert.equal(frames.filter(frame => frame.type === terminal).length, 1, stderr);
  if (scenario === 'useful') {
    assert.equal(code, 0);
    const progress = frames.filter(frame => frame.type === 'progress');
    assert.ok(new Set(progress.map(frame => frame.message)).size >= 2);
  } else {
    assert.equal(frames.some(frame => frame.type === 'result'), false);
    if (scenario === 'malformed') assert.notEqual(code, 0);
  }
  return { scenario, exit_code: code, terminal, observed_network_requests: 0 };
}
const outcomes = [];
for (const scenario of ['useful', 'cancelled', 'malformed']) outcomes.push(await run(scenario));
console.log(JSON.stringify({ source_sha: process.env.SOURCE_SHA, fixture_sha256: process.env.FIXTURE_HASH, image_digest: process.env.IMAGE_DIGEST, legacy_binary_syntax_and_selectors: 'pass', outcomes }));
