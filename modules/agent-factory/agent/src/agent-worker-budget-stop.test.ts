/**
 * Contract tests for the worker's spend-cap stop detection (issue #4187).
 *
 * When the gateway refuses a model call with HTTP 402 because a per-run or
 * per-chain cap is exhausted, the run is over — 402 is chosen precisely because
 * nothing retries it. The signal used to die at the process boundary, so the run
 * was recorded as a generic `failed` with a stack trace and nobody could tell a
 * cap firing from a crash.
 *
 * These assertions run against the SOURCE TEXT of `agent-worker.ts` rather than
 * by importing it: that module calls `main()` at load and exports nothing, so an
 * import would execute the agent. Six sibling suites (`-aidlc-gating`,
 * `-branch-naming`, `-coding-guidelines`, `-presubmit-checks`, `-prompt-order`,
 * `-installation-resolution`) use the same technique for the same reason.
 *
 * Jest globals are used directly — this project runs ts-jest, and importing from
 * 'vitest' is what makes several sibling suites fail to compile.
 */

import * as fs from 'fs';
import * as path from 'path';

const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

/** The body of `detectBudgetStop`, isolated so unrelated code cannot match. */
const DETECT_FN = (() => {
  const open = source.indexOf('function detectBudgetStop');
  expect(open).toBeGreaterThan(-1);
  const end = source.indexOf('\n}', open);
  expect(end).toBeGreaterThan(open);
  return source.slice(open, end);
})();

describe('agent-worker budget-stop detection (Issue #4187)', () => {
  describe('recognising the denial', () => {
    it('requires BOTH the 402 status and the error code', () => {
      // Either alone is not enough: an unrelated error whose text happens to
      // contain "402" (a token count, a request id) must not be reported as a
      // budget stop, which would mislabel a real crash as a spend cap.
      expect(DETECT_FN).toContain("includes('402')");
      expect(DETECT_FN).toContain("includes('budget_exceeded')");
      expect(DETECT_FN).toMatch(/!lower\.includes\('402'\)\s*\|\|\s*!lower\.includes\('budget_exceeded'\)/);
    });

    it('lowercases the message before matching', () => {
      // The body casing is not part of any contract, so matching case-sensitively
      // would make detection depend on an incidental detail of the error text.
      expect(DETECT_FN).toContain('toLowerCase()');
    });

    it('returns null when this is not a budget stop', () => {
      // The normal path. Every failing run reaches this function, so a false
      // positive would relabel unrelated crashes as budget stops.
      expect(DETECT_FN).toContain('return null;');
    });
  });

  describe('the scope discriminator', () => {
    it('reads the gateway\'s scope field to tell a run cap from a chain cap', () => {
      // These are different remedies — one run overspent vs. a fan-out overspent
      // in aggregate — so the two must not collapse into one outcome.
      expect(DETECT_FN).toContain('"scope"');
      expect(DETECT_FN).toContain('run_cap_exceeded');
      expect(DETECT_FN).toContain('chain_cap_exceeded');
    });

    it('falls back to the hierarchy cap when no scope is present', () => {
      // A bare `budget_exceeded` is an org/team/user cap, which predates #4187.
      // Without this branch those denials would keep landing as plain failures.
      expect(DETECT_FN).toContain('hierarchy_cap_exceeded');
    });

    it('maps the root-human scope to its own reason (Issue #4300)', () => {
      // A per-root-human cap is a THIRD scope, and it is the one with a
      // different remedy: not "this run overspent" but "the person who started
      // this is out of budget for the period". Collapsing it into
      // `hierarchy_cap_exceeded` — which is what the pre-#4300 fallback does —
      // tells the operator to look at an org/team cap that is not the one that
      // fired, and the discriminator is a closed regex so a new scope reaches
      // the fallback silently unless the regex is widened too.
      expect(DETECT_FN).toContain('root_user');
      expect(DETECT_FN).toContain('root_user_cap_exceeded');
      // The scope regex must actually admit the value; matching only the
      // ternary arm would pass while the regex still rejected "root_user".
      const scopeRegex = /\/"scope"[^/]*\//.exec(DETECT_FN)?.[0] ?? '';
      expect(scopeRegex).toContain('root_user');
    });

    it('keeps the three scopes distinct from each other and from the fallback', () => {
      // Four outcomes, four distinct strings. A copy-paste that reused
      // `chain_cap_exceeded` for the new arm would still satisfy the assertions
      // above.
      const reasons = ['run_cap_exceeded', 'chain_cap_exceeded', 'root_user_cap_exceeded', 'hierarchy_cap_exceeded'];
      expect(new Set(reasons).size).toBe(4);
      for (const reason of reasons) {
        expect(DETECT_FN).toContain(reason);
      }
    });

    it('emits static enums, never prose', () => {
      // Same contract as skip_reason (#4020): the producer ships an enum and the
      // frontend owns the wording, so changing the text does not require
      // rebuilding and redeploying the agent image.
      expect(DETECT_FN).not.toMatch(/stopReason\s*=\s*['"`][A-Z]/);
      expect(DETECT_FN).not.toContain('spend cap.');
    });
  });

  describe('recording the outcome', () => {
    it('writes budget_stopped and the reason to the result-metadata bridge', () => {
      // Python owns the DynamoDB write because it holds both halves of the row
      // key; Node's only channel is this file.
      expect(source).toContain('budget_stopped: true');
      expect(source).toMatch(/writeResultMetadata\(\{\s*budget_stopped: true,\s*stop_reason:/);
    });

    it('still re-throws so the run remains a non-zero exit', () => {
      // The bookkeeping is additive. Swallowing the error would report the run as
      // a success that simply stopped early, and the SQS message handling
      // downstream depends on the failure propagating.
      const catchIdx = source.indexOf('const budgetStop = detectBudgetStop(err);');
      expect(catchIdx).toBeGreaterThan(-1);
      expect(source.slice(catchIdx)).toMatch(/throw error;/);
    });

    it('logs at WARN, not ERROR', () => {
      // A cap doing its job is not an incident. ERROR here would page on correct
      // behaviour and bury the failures that do need attention.
      const catchIdx = source.indexOf('const budgetStop = detectBudgetStop(err);');
      expect(source.slice(catchIdx, catchIdx + 400)).toContain("log('WARN'");
    });
  });
});
