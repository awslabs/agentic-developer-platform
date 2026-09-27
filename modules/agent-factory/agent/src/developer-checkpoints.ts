import type { HookCallback, HookInput } from '@anthropic-ai/claude-agent-sdk';
import { createSpillHookCallback, SpillHookOptions } from './utils/spill';
import type { ClaudePauseHooks } from './harnesses/claude-control';

const CHECKPOINT_INTERVAL_MS = 15 * 60 * 1000;

export function developerCheckpointGuidance(agentType: string): string {
  if (agentType !== 'developer') return '';
  return `
### Developer progress

For a repair, reproduce the reported command through its real entrypoint early.
Use the provided evidence and existing test fixtures before building new mock
servers. Batch related reads; do not repeatedly reread files already understood.
Within 15 minutes, produce a bounded reproduction, a focused regression/change,
or a concrete blocker with the missing evidence. If an experiment fails to
answer the question, change approach instead of repeating the same investigation.
An engine implementation run with no repository changes is stopped after 30
minutes. Do not make empty edits to satisfy that bound: save useful evidence or
report why implementation cannot proceed. Temporary files and heartbeats are not
an implementation. Use short explicit command timeouts; long validation may use
up to 10 minutes per command and must follow a saved checkpoint.

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
        additionalContext: 'Progress check: identify the reproduced failure, what changed since the last check, and the next bounded action. If investigation has produced no change, save a focused regression or report the concrete missing evidence; do not continue rereading the same files. Scheduled developer checkpoint check: if this is authorized ' +
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
 * hook composed before developer command bounds; its settle edge shares
 * `PostToolUse` with the two above and so joins the merged callback. It returns
 * no `hookSpecificOutput` of its own, so it cannot displace either.
 */
export function createWorkerToolHooks(
  opts: SpillHookOptions & { agentType: string; pauseHooks?: ClaudePauseHooks },
) {
  const spill = createSpillHookCallback(opts);
  const remind = checkpointReminder(opts.agentType);
  const pause = opts.pauseHooks;
  const started = new Map<string, number>();
  const finished = (input: HookInput, failed: boolean) => {
    if (!('tool_use_id' in input)) return;
    const began = started.get(input.tool_use_id);
    started.delete(input.tool_use_id);
    if (began !== undefined) opts.log?.(`Tool completed: ${input.tool_name} duration_ms=${Date.now() - began} failed=${failed}`);
  };
  return {
    ...(pause
      ? {
          // Background work behind finished tools, which is the one thing the
          // barrier cannot see for itself.
          Stop: [{ hooks: [pause.onStop] }],
          SubagentStop: [{ hooks: [pause.onStop] }],
        }
      : {}),
    ...((pause || opts.agentType === 'developer') ? {
      // Preserve the pause adapter's timeout so a held gate outlasts SDK defaults.
      PreToolUse: [{ timeout: pause?.preToolUseTimeoutSeconds ?? 10, hooks: [async (
        input: HookInput, toolUseID?: string, options?: { signal: AbortSignal },
      ) => {
        const admission = await pause?.preToolUse(input, toolUseID, options) ?? {};
        if (input.hook_event_name !== 'PreToolUse') return admission;
        const prior = admission.hookSpecificOutput as Record<string, unknown> | undefined;
        if (prior && 'permissionDecision' in prior && prior.permissionDecision === 'deny') return admission;
        if (started.size >= 256) started.delete(started.keys().next().value!);
        started.set(input.tool_use_id, Date.now());
        if (opts.agentType !== 'developer' || input.tool_name !== 'Bash') return admission;
        const original = input.tool_input as Record<string, unknown>;
        const supplied = original.timeout;
        const timeout = typeof supplied === 'number' && Number.isFinite(supplied) && supplied > 0
          ? Math.min(supplied, 600_000) : 120_000;
        return { ...admission, hookSpecificOutput: {
          ...prior, hookEventName: 'PreToolUse' as const,
          updatedInput: { ...original, ...((prior?.updatedInput as Record<string, unknown> | undefined) ?? {}), timeout },
        } };
      }] }],
    } : {}),
    PostToolUseFailure: [{ hooks: [async (input: HookInput) => {
      finished(input, true);
      return await pause?.postToolUse(input) ?? {};
    }] }],
    PostToolUse: [{ hooks: [async (input: HookInput, toolUseID: string | undefined,
      options: { signal: AbortSignal }) => {
      finished(input, false);
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
