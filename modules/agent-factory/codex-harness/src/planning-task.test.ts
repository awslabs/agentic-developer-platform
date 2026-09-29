/** Exercise the packaged entrypoint, official SDK, host IPC and clarification replay. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';

test('Task intent refinement applies a replayed clarification once through the SDK host', { timeout: 30000 }, async () => {
  const start = JSON.parse(await readFile(new URL('../../../../docs/task-api/contracts/v1/fixtures/valid/process-codex-start-frame.json', import.meta.url), 'utf8'));
  delete start.$fixture;
  start.deadline_at = new Date(Date.now() + 25000).toISOString();
  start.harness.policy.deadlineMs = Date.parse(start.deadline_at);
  start.instructions = 'Preserve audit history; clarify retention.';
  start.inputs = {}; start.artifacts = [];
  const child = spawn(process.execPath, [new URL('./task-entry.mjs', import.meta.url).pathname, '--embedded'], {
    env: { ...process.env, ADP_TASK_NETWORK: 'host-mediated-sdk' }, stdio: ['pipe', 'pipe', 'pipe'],
  });
  const frames: any[] = []; let stderr = '', calls = 0;
  child.stderr.on('data', chunk => { stderr += chunk; });
  const send = (type: string, fields: object) => child.stdin.write(JSON.stringify({protocol_version: 1, request_id: randomUUID(), task_id: start.task_id, type, ...fields}) + '\n');
  const command = randomUUID();
  const artifact = { summary: 'Audit draft', requirements: [{id: 'history', text: 'Preserve audit history', source_refs: ['instructions']}], assumptions: [], open_questions: [], superseded_requirements: [], draft: {intent: 'Preserve audit history'} };
  const exited = new Promise<number | null>(resolve => child.on('exit', resolve));
  try {
    child.stdin.write(JSON.stringify(start) + '\n');
    for await (const line of createInterface({ input: child.stdout })) {
      const frame = JSON.parse(line); frames.push(frame);
      if (frame.type === 'control.request') send('control.result', {request_id: frame.request_id, current: true});
      if (frame.type === 'model.request') {
        calls++;
        const output = calls === 1 ? {artifact, clarification: 'How long should history be retained?'} : {
          artifact: {...artifact, requirements: [...artifact.requirements, {id: 'retention', text: 'Retain history for 90 days', source_refs: [`follow_up_input.${command}`]}], draft: {...artifact.draft, constraints: ['Retain for 90 days']}}, clarification: null,
        };
        send('model.result', {turn_id: frame.turn_id, operation_status: 'confirmed', content: [], stop_reason: 'completed', responses_response: {
          id: `resp_${calls}`, status: 'completed', output: [{id: `msg_${calls}`, type: 'message', role: 'assistant', status: 'completed', content: [{type: 'output_text', text: JSON.stringify(output), annotations: []}]}], usage: {input_tokens: 10, output_tokens: 50},
        }});
      }
      if (frame.type === 'input.required') {
        const turn = {turn_id: randomUUID(), messages: [{command_id: command, text: 'Retain for 90 days'}]};
        send('turn', turn); send('turn', turn);
      }
    }
    assert.equal(await exited, 0, JSON.stringify(frames.at(-1)) + stderr);
    assert.equal(calls, 2);
    assert.equal(frames.filter(f => f.type === 'input.required').length, 1);
    const report = frames.find(f => f.type === 'result')?.report;
    assert.ok(report, JSON.stringify(frames));
    assert.equal(JSON.parse(report.documents[0].content).requirements.length, 2);
    assert.equal(report.evidence_refs.filter((ref: any) => ref.ref === `follow_up_input.${command}`).length, 1);
  } finally { child.kill('SIGKILL'); }
});
