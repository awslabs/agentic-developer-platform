#!/usr/bin/env node
/** Embedded Task lifecycle. Stdout is bounded IPC; no child-owned credentials. */
import { ArtifactTransfers } from './task-contracts/artifact-transfer.js';
import { parseHostFrame, assertInvestigatorReport } from './task-contracts/protocol.js';
import { HostBridge, decode, encode, MAX_FRAME_BYTES } from './task-sdk/protocol.mjs';
import { taskHarness, parseTaskReport } from './task-adapter.js';
import { runAdmittedSession } from './session.js';
import { TaskTools } from './task-tools.js';
import { startTelemetry, activeTraceparent } from './telemetry.js';

console.log = console.info = console.debug = () => {};
const transfers = new ArtifactTransfers();
let bridge, start, terminal = false, buffer = Buffer.alloc(0);
const write = value => process.stdout.write(encode(value));

function finish(error, report) {
  if (terminal) return;
  terminal = true;
  if (bridge) {
    if (bridge.cancelCommand) bridge.send('cancelled', { command_id: bridge.cancelCommand, partial_findings: null });
    else if (error) bridge.send('error', { code: bridge.failure?.message === 'model_outcome_unknown' ? 'model_outcome_unknown' : 'process_failed', message: 'Codex Task did not complete with validated output and current authority.' });
    else bridge.send('result', { report });
  }
  bridge?.fail(error ?? new Error('task lifecycle ended'));
  process.stdin.destroy();
  process.exitCode = error && !bridge?.cancelCommand ? 1 : 0;
}

async function run() {
  const { snapshot, policy, tools, repository } = taskHarness(start);
  policy.deadlineMs = Math.min(policy.deadlineMs, Date.now() + Math.min(policy.limits.maxDurationMs, JSON.parse(snapshot.definition).limits.maxDurationMs));
  bridge.progress('Validated the task and persona bindings.', 'evidence_inventory');
  const amendments = [];
  let operations = 0;
  const maxOperations = Math.min(start.limits.max_turns, policy.limits.maxTurns, JSON.parse(snapshot.definition).limits.maxTurns);
  const taskTools = tools.length ? new TaskTools(tools, bridge, maxOperations, repository?.capabilities ?? []) : undefined;
  let repair = false;
  let previous;
  const input = { instructions: start.instructions, inputs: start.inputs ?? {}, acceptance_criteria: start.acceptance_criteria ?? [], artifacts: start.artifacts ?? [] };
  const outputContract = 'Return only a JSON Task report with summary (string), findings (array of {statement,evidence_refs,confidence?}), uncertainties (string array), recommendations (string array), and evidence_refs (exact objects from the supplied evidence list). Every finding must cite existing evidence. Do not invent actions, tests, approvals, artifacts or citations. Address every acceptance criterion; state unmet requirements and missing evidence as uncertainties.';
  for (;;) {
    if (operations >= maxOperations) throw new Error('task model budget exhausted');
    amendments.push(...bridge.takeSteering());
    const toolSession = taskTools?.session();
    const evidence = await runAdmittedSession({
      ...(repository ? { repository: repository.binding } : {}),
      runId: start.invocation_id, snapshot, policy, source: { kind: 'task-api', taskId: start.task_id, generation: start.generation },
      prompt: JSON.stringify({ task: input, amendments, evidence_refs: [...bridge.evidence.values()], output_contract: outputContract,
        ...(repair ? { correction: 'Previous output failed the report schema or cited unsupported evidence. Produce a corrected grounded report.', previous_output: previous } : {}) }),
      maxOutputTokens: start.limits.max_output_tokens_per_turn, maxResponseBytes: 48000, signal: bridge.controller.signal,
    }, {
      ...(toolSession ? { toolBroker: toolSession.toolBroker } : {}),
      ...(JSON.parse(snapshot.definition).completionPolicy === 'validated-change' ? {
        async verifyCompletion(signal) { signal.throwIfAborted(); return bridge.completion(); },
      } : {}),
      async assertCurrent(signal) { signal.throwIfAborted(); await bridge.current(); signal.throwIfAborted(); },
      async model(request, signal) {
        signal.throwIfAborted();
        if (++operations > maxOperations) throw new Error('task model operation budget exhausted');
        return toolSession ? toolSession.model(request, signal) : bridge.responses(request);
      },
      async progress(event) { if (event.type === 'turn.started') bridge.progress('Analysing the admitted task and supplied evidence.', 'analysis'); },
    });
    if (bridge.steering.length) {
      if (operations >= maxOperations) throw new Error('task amendments require more model budget');
      repair = false;
      continue;
    }
    let report;
    try { report = parseTaskReport(evidence.response, bridge.evidence, assertInvestigatorReport); }
    catch (error) {
      if (repair || operations >= maxOperations) throw error;
      repair = true; previous = evidence.response;
      bridge.progress('Checking and correcting the report structure and citations.', 'synthesis');
      continue;
    }
    bridge.progress('Validated the report structure and evidence references.', 'synthesis');
    // A final host read delivers any pending input before a result is emitted.
    await bridge.current();
    if (bridge.steering.length) { repair = false; continue; }
    return report;
  }
}

if (!process.argv.includes('--embedded') || process.env.ADP_TASK_NETWORK !== 'host-mediated-sdk') {
  process.stderr.write('Codex Task requires the trusted host lane.\n');
  process.exitCode = 64;
} else {
  process.stdin.on('data', chunk => {
    try {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.includes(10)) {
        const boundary = buffer.indexOf(10);
        if (boundary + 1 > MAX_FRAME_BYTES) throw new Error('frame bound');
        const line = buffer.subarray(0, boundary).toString('utf8'); buffer = buffer.subarray(boundary + 1);
        if (!line.trim()) continue;
        const value = decode(line);
        if (value.type === 'artifact.chunk') { if (start) throw new Error('late artifact'); transfers.accept(parseHostFrame(line)); }
        else if (value.type === 'start') {
          if (start) throw new Error('repeated start');
          const { harness, model_binding, persona, deadline_at, repository, ...task } = value;
          start = { ...transfers.start(parseHostFrame(JSON.stringify(task), 32)), harness, model_binding, persona, deadline_at, repository };
          taskHarness(start);
          bridge = new HostBridge(start, write, { allowSteering: true, traceContext: activeTraceparent });
          bridge.send('ready', { capabilities: ['input', 'cancel'] });
          const telemetry = startTelemetry({ endpoint: process.env.ADP_CODEX_OTEL_ENDPOINT, traceparent: harness.traceparent,
            runId: start.task_id, persona: start.persona });
          telemetry.run(run).then(async report => { await telemetry.shutdown(); finish(null, report); },
            async error => { await telemetry.shutdown(); bridge.failure ??= error; finish(error); });
        } else {
          if (!bridge) throw new Error('missing start');
          if (!terminal) bridge.receive(value);
        }
      }
      if (buffer.length > MAX_FRAME_BYTES) throw new Error('frame bound');
    } catch (error) { bridge?.fail(error); finish(error); }
  });
  process.stdin.on('end', () => { if (!terminal) { const error = new Error('host disconnected'); bridge?.fail(error); finish(error); } });
}
