/**
 * Comment history must not be described as carrying decisions or approvals
 * (issue #4204, R-N3a, AC-25).
 *
 * Promotion state lives in the orchestration store, where a state change is
 * guarded by `transition()` and recorded as an append-only decision row. A
 * GitHub issue comment is none of those things: anyone who can comment can write
 * one, and nothing verifies who did. Telling an agent that the comment history
 * "may contain decisions or approvals" invites it to treat a comment as the
 * authority for advancing work — which is a forgeable promotion signal reachable
 * by anyone with write access to the repo.
 *
 * The comments stay in the prompt; they are genuinely useful context. What is
 * removed is the claim that they carry authority.
 *
 * This is asserted at SOURCE level rather than by rendering a prompt because the
 * strings are inline template literals in two large prompt builders whose other
 * inputs (a live issue, a memory context, a resolved skill) would have to be
 * constructed to reach them. A source assertion tests the same bytes that ship,
 * and it also catches a NEW site added elsewhere in either file, which a
 * render-one-prompt test would miss.
 */
import * as fs from 'fs';
import * as path from 'path';

const SRC_ROOT = path.resolve(__dirname, '../..');

/** The prompt builders that embed GitHub comment history. */
const FILES_WITH_COMMENT_HISTORY = ['agent-worker.ts', 'skill-agent.ts'];

/**
 * Words that assert authority. Matched only on lines that introduce the comment
 * history, so unrelated prose elsewhere in these files is not in scope — for
 * example a legitimate reference to the orchestration decisions table.
 */
const AUTHORITY_WORDS = /\b(decisions|approvals|approved|authorised|authorized)\b/i;

/** Lines that introduce the comment-history block to the agent. */
const COMMENT_HISTORY_INTRO = /comments?\s+(have been posted|on this issue)|previous comments/i;

describe('comment-history preamble (AC-25)', () => {
  for (const file of FILES_WITH_COMMENT_HISTORY) {
    describe(file, () => {
      const source = fs.readFileSync(path.join(SRC_ROOT, file), 'utf8');

      it('does not describe comment history as carrying decisions or approvals', () => {
        const offenders = source
          .split('\n')
          .map((line, index) => ({ line, lineNumber: index + 1 }))
          .filter(({ line }) => COMMENT_HISTORY_INTRO.test(line) && AUTHORITY_WORDS.test(line));

        expect(
          offenders.map(({ lineNumber, line }) => `${file}:${lineNumber}: ${line.trim()}`),
        ).toEqual([]);
      });

      it('still tells the agent the comments are context worth reading', () => {
        // The removal must not have deleted the block outright — losing useful
        // context is a different regression from the one AC-25 asks for.
        const intros = source.split('\n').filter((line) => COMMENT_HISTORY_INTRO.test(line));
        expect(intros.length).toBeGreaterThan(0);
        for (const intro of intros) {
          expect(intro).toMatch(/context|research/i);
        }
      });
    });
  }
});
