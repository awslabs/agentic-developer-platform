/**
 * Live-acceptance gate for a produced capture — Issue #5878.
 *
 * Run this before citing a `browser_control_run.json` as Wave 4 acceptance
 * evidence:
 *
 *   node --experimental-strip-types tests/e2e/agent-control-gate.ts <capture.json>
 *
 * Exits 0 only if the capture is admissible as live evidence. Exits 1, naming the
 * specific reason, if it is not.
 *
 * ---------------------------------------------------------------------------
 * Why this is a separate executable rather than a check inside the producer
 * ---------------------------------------------------------------------------
 *
 * The producer already refuses to WRITE an incomplete capture. This gate answers
 * a different question, asked later and usually by somebody else: a file is being
 * offered as proof that the controls work against a real deployment — may it be?
 *
 * That question has to be answerable about a file on disk, by a reviewer or a
 * pipeline that did not run the browser. A mocked capture is a perfectly valid
 * artifact — it is genuine evidence that the real bundle renders and wires up
 * correctly — and it is simply not evidence about a gateway. Without something
 * executable to say so, the distinction lives only in prose, and the failure mode
 * this story exists to prevent is precisely a plausible-looking artifact being
 * read as more than it is.
 *
 * `verifyForLiveAcceptance` held that logic already but had no caller, which made
 * it a guard nobody ran.
 */

import { readFileSync } from 'node:fs';

// Extension included deliberately. The spec files are resolved by Playwright's
// bundler, which tolerates an extensionless import; this script is run by node
// directly, whose ESM resolver does not.
import { CaptureInputError, CaptureIncompleteError, verifyForLiveAcceptance } from './agent-control-capture.ts';

function main(argv: string[]): number {
  const path = argv[2];
  if (!path) {
    process.stderr.write('usage: agent-control-gate.ts <browser_control_run.json>\n');
    return 2;
  }

  let capture: Record<string, unknown>;
  try {
    capture = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    process.stderr.write(`REFUSED: ${path} could not be read as JSON: ${error}\n`);
    return 1;
  }

  try {
    verifyForLiveAcceptance(capture);
  } catch (error) {
    if (error instanceof CaptureInputError || error instanceof CaptureIncompleteError) {
      process.stderr.write(`REFUSED as live acceptance evidence: ${error.message}\n`);
      return 1;
    }
    throw error;
  }

  // Deliberately terse on success, and deliberately still says what was checked:
  // a reviewer reading CI output should not have to infer which claim passed.
  process.stdout.write(
    `ADMISSIBLE as live acceptance evidence: ${path}\n` +
      `  mode=live, served bundle matched against a deployment receipt, ` +
      `no control response injected.\n`,
  );
  return 0;
}

process.exit(main(process.argv));
