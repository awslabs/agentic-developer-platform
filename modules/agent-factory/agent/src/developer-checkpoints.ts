import type { HookCallback, HookInput } from '@anthropic-ai/claude-agent-sdk';
import { createSpillHookCallback, SpillHookOptions } from './utils/spill';

const CHECKPOINT_INTERVAL_MS = 15 * 60 * 1000;

export function developerCheckpointGuidance(agentType: string): string {
  if (agentType !== 'developer') return '';
  return `
### Developer branch checkpoints

For authorized implementation work, put the checkpoint strategy after the
plain-language task understanding and logical approach in the initial
plan comment and any required code plan. Include the target branch, first useful
milestone, later milestones, and approximately 15-minute cadence.
Example of the supporting checkpoint detail, after explaining the task and
approach: "On agent/issue-N, I will first publish the change that saves an account
connection, then the checks that the same user can list and remove it. I will
checkpoint about every 15 minutes at safe boundaries and before long validation,
share commit links, and open a ready PR after completing the assignment and
pre-submit checks."

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
- Do not create draft PRs, including when older issue text asks for one. Share
  branch/commit links while work is in progress. Open a ready PR only after the
  agreed implementation, integration, tests and documentation are complete and
  pre-submit checks pass, with any verified pre-existing failures documented.
  Reuse an existing PR; if it is a draft, mark it ready only at that same point.
  A checkpoint does not mark the story done, dispatch review/evaluation, advance
  a wave, merge, or bypass AI-DLC approvals. Continue the assignment after pushing;
  if blocked, report the blocker and remaining work without declaring completion.
- If no new changes are ready, report progress and the reason instead of an empty
  commit. If publication fails, preserve local work and report the actual failure;
  do not claim a successful checkpoint or spend the run in an unbounded retry loop.
- Read-only tasks need no commits or PR.
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
          'Share branch/commit links; do not create a PR until implementation and ' +
          'pre-submit checks are complete. Continue the assignment after the checkpoint. If there is no new ' +
          'work, a writer is still active, or publication is blocked, report that instead. ' +
          'This reminder neither verifies a push nor authorizes implementation, review, ' +
          'wave advancement or any action past an approval gate.',
      },
    };
  };
}

/** Compose explicitly so a reminder cannot discard a spilled tool-output locator. */
export function createWorkerToolHooks(opts: SpillHookOptions & { agentType: string }) {
  const spill = createSpillHookCallback(opts);
  const remind = checkpointReminder(opts.agentType);
  return {
    PostToolUse: [{ hooks: [async (input: HookInput, toolUseID: string | undefined,
      options: { signal: AbortSignal }) => {
      const reminder = await remind(input, toolUseID, options);
      const spilled = await spill(input);
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
