#!/usr/bin/env node
import { ArtifactTransfers } from '../../investigator/dist/artifact-transfer.js';
import { parseHostFrame, assertInvestigatorReport } from '../../investigator/dist/protocol.js';
import { HostBridge, decode, encode, frame, MAX_FRAME_BYTES } from './protocol.mjs';
import { runCyber } from './driver.mjs';
import { runCoding } from './coding-driver.mjs';
import { runCodexCoding } from './codex-driver.mjs';

console.log = () => {}; // stdout is IPC only, including SDK dependencies.
let bridge, started = false, terminal = false, buffer = Buffer.alloc(0);
const artifacts = new ArtifactTransfers();
function write(value) { process.stdout.write(encode(value)); }
function finish(error, report) {
  if (terminal) return;
  if (!error && !bridge?.cancelCommand) {
    try { assertInvestigatorReport(report); encode(frame('result', bridge.start.task_id, { report })); } catch (validationError) { error = validationError; }
  }
  terminal = true;
  if (bridge) {
    if (bridge.cancelCommand) bridge.send('cancelled', { command_id: bridge.cancelCommand, partial_findings: null });
    else if (error) bridge.send('error', { code: bridge.failure?.message === 'model_outcome_unknown' ? 'model_outcome_unknown' : 'process_failed', message: error.message === 'SDK model-turn limit reached before a grounded report was accepted' ? error.message : 'Cyber SDK execution did not complete with confirmed evidence.' });
    else { bridge.send('result', { report }); }
  }
  process.stdin.destroy();
  process.exitCode = error && !bridge?.cancelCommand ? 1 : 0;
}
if (!process.argv.includes('--embedded') || process.env.ADP_TASK_NETWORK !== 'host-mediated-sdk') {
  process.stderr.write('Cyber SDK requires the trusted Task host lane.\n'); process.exitCode = 64;
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
        if (value.type === 'artifact.chunk') { if (started) throw new Error('late artifact'); artifacts.accept(parseHostFrame(line)); }
        else if (value.type === 'start') {
          if (started) throw new Error('repeated start'); started = true;
          const start = artifacts.start(parseHostFrame(line));
          bridge = new HostBridge(start, write);
          bridge.send('ready', { capabilities: ['input', 'cancel'] });
          (process.argv.includes('--codex') ? runCodexCoding : process.argv.includes('--developer') ? runCoding : runCyber)(start, bridge).then(report => finish(null, report), error => finish(error));
        } else { if (!bridge) throw new Error('missing start'); bridge.receive(value); }
      }
      if (buffer.length > MAX_FRAME_BYTES) throw new Error('frame bound');
    } catch (error) { bridge?.fail(error); finish(error); }
  });
  process.stdin.on('end', () => { if (!terminal) { const error = new Error('host disconnected'); bridge?.fail(error); finish(error); } });
}
