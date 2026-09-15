import type { HookCallback, HookInput } from '@anthropic-ai/claude-agent-sdk';
import { createSpillHookCallback, SpillHookOptions } from './utils/spill';
import type { ClaudePauseHooks } from './harnesses/claude-control';

const CHECKPOINT_INTERVAL_MS = 15 * 60 * 1000;

export function developerCheckpointGuidance(agentType: string): string {
  if (agentType !== 'developer') return '';
  return `
### Developer branch checkpoints

For authorized implementation work, include a checkpoint strategy in the initial
plan comment and any required code plan: target branch, first useful milestone,
later milestones, approximately 15-minute cadence, and draft PR approach.
Example: "On agent/issue-N, I will push the API contract first, then persistence
and tests, checkpointing about every 15 minutes at safe boundaries and before
long validation. I will link a draft PR and report checks still pending."

- Push the first coherent change, then checkpoint at meaningful milestones and
  about every 15 minutes at a safe tool boundary while changes accumulate. Push
  useful work before starting a long test suite or investigation. Do not wait
  for the full suite to pass to publish an explicitly incomplete checkpoint.
- Inspect git status and the diff; stage only intended task files, inspect the
  staged diff for secrets and unrelated/generated files, and commit with a
  descriptive message. Run quick relevant checks when practical. Push normally
  to the task branch; never force-push, rewrite shared history or create empty
  commits just to show activity. Do not mutate Git while another writer or Git
  operation is active. A timer reminder is not permission to snapshot blindly.
- Verify the pushed commit against the remote branch (git ls-remote origin
  refs/heads/<branch> versus git rev-parse HEAD). Only then call it a published
  checkpoint. Report the commit link, completed scope, remaining work and checks
  passed/failed/not run in a concise progress update on the designated issue.
- Reuse an existing PR. Open an early draft PR after the first useful push only
  when repository automation is known to keep drafts out of review/evaluation
  and wave advancement. Record incomplete work and check status. If that behavior
  is unknown, or policy disallows drafts or treats any PR as a handoff, publish
  the branch/commit link instead and state that in the plan. A checkpoint does not mark the story done,
  dispatch review/evaluation, advance a wave, merge, or bypass AI-DLC approvals.
- If no new changes are ready, report progress and the reason instead of an empty
  commit. If publication fails, preserve local work and report the actual failure;
  do not claim a successful checkpoint or spend the run in an unbounded retry loop.
- Before marking a PR ready for review, complete the pre-submit checks below and
  document any pre-existing failures. Read-only tasks need no commits or draft PR.
`;
}

/** Remind at tool boundaries, never run Git in the background. State is per run. */
function checkpointReminder(agentType: string): HookCallback {
  let lastReminderAt = Date.now();
  return async (input) => {
    if (agentType !== 'developer' || input.agent_id ||
        input.hook_event_name !== 'PostToolUse') return {};
    const now = Date.now();
    if (now - lastReminderAt < CHECKPOINT_INTERVAL_MS) return {};
    lastReminderAt = now;
    return {
      hookSpecificOutput: {
        hookEventName: 'PostToolUse',
        additionalContext: 'Scheduled developer checkpoint check: if this is authorized ' +
          'implementation work and changes have accumulated since the last verified push, ' +
          'publish a coherent checkpoint at the next safe boundary, following the branch ' +
          'checkpoint strategy in your plan. Inspect and selectively stage the diff; verify ' +
          'the remote commit, then report the link, remaining work and check status. ' +
          'Do not wait for full validation to publish draft progress. If there is no new ' +
          'work, a writer is still active, or publication is blocked, report that instead. ' +
          'This reminder neither verifies a push nor authorizes implementation, review, ' +
          'wave advancement or any action past an approval gate.',
      },
    };
  };
}

/**
 * Compose explicitly so a reminder cannot discard a spilled tool-output locator.
 *
 * The single-callback merge is deliberate, not stylistic. Handing the SDK two
 * array entries for `PostToolUse` would put the merge semantics in the CLI's
 * hands, and the field at risk is `updatedToolOutput` — the locator that replaces
 * a spilled blob. Lose it and the model receives the original oversized output,
 * which is the exact failure spilling exists to prevent.
 *
 * `pause` (#3961) composes into the same shape. Its barrier is a `PreToolUse`
 * hook, which is unoccupied and therefore additive; its settle edge shares
 * `PostToolUse` with the two above and so joins the merged callback. It returns
 * no `hookSpecificOutput` of its own, so it cannot displace either.
 */
export function createWorkerToolHooks(
  opts: SpillHookOptions & { agentType: string; pauseHooks?: ClaudePauseHooks },
) {
  const spill = createSpillHookCallback(opts);
  const remind = checkpointReminder(opts.agentType);
  const pause = opts.pauseHooks;
  return {
    ...(pause
      ? {
          // The admission barrier. Nothing else claims PreToolUse, so this is a
          // straight addition rather than a merge.
          //
          // `timeout` is not optional in practice, even though the type allows it.
          // The CLI enforces hook timeouts on its side and applies its own default
          // when a matcher omits one; this hook is *designed* to block for as long
          // as an operator holds the pause. Leaving the bound implicit means an
          // undocumented default decides whether pause works at all, and if it is
          // shorter than the budget every long pause aborts its parked tool, breaches
          // the barrier and degrades to `unavailable`. The adapter derives the value
          // from the gate's own budget so the two cannot drift.
          PreToolUse: [{ hooks: [pause.preToolUse], timeout: pause.preToolUseTimeoutSeconds }],
          // Both edges settle the admission: a tool that failed has stopped
          // running just as surely as one that succeeded, and treating only
          // success as an ending would leave a failed tool's admission
          // outstanding forever — every later pause would then wait on it and
          // never confirm.
          PostToolUseFailure: [{ hooks: [pause.postToolUse] }],
          // Background work behind finished tools, which is the one thing the
          // barrier cannot see for itself.
          Stop: [{ hooks: [pause.onStop] }],
          SubagentStop: [{ hooks: [pause.onStop] }],
        }
      : {}),
    PostToolUse: [{ hooks: [async (input: HookInput, toolUseID: string | undefined,
      options: { signal: AbortSignal }) => {
      const reminder = await remind(input, toolUseID, options);
      const spilled = await spill(input);
      // Settle before returning, so the admission is released even if the two
      // above produced nothing to merge.
      await pause?.postToolUse(input);
      if (!('hookSpecificOutput' in reminder)) return spilled;
      return {
        ...spilled,
        hookSpecificOutput: {
          ...(spilled.hookSpecificOutput as Record<string, unknown> | undefined),
          ...reminder.hookSpecificOutput,
        },
      };
    }] }],
  };
}
