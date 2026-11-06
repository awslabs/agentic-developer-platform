import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { operationsStoryTools } from './operations-story-tools';
import { spawnSdkWithoutAppKey } from './github-runtime-auth';
import { operationsQuery } from './operations-story-control';
import { isControlCancellation } from './control-runtime';

export function storyPrompt(context: { title: string; workflows: string[]; issue: number }): string {
  if (typeof context.title !== 'string' || context.title.length > 300 ||
      !Array.isArray(context.workflows) || context.workflows.length > 20 ||
      context.workflows.some(value => typeof value !== 'string' || !/^[A-Za-z0-9_.-]+\.ya?ml$/.test(value))) {
    throw new Error('Invalid operations story context');
  }
  const instructions = readFileSync(join(process.env.ADP_RULES_DIR || '/app/codex-harness/rules', 'personas', 'operations-story.md'), 'utf8');
  return `${instructions}\n\nContinue the automatically restored story board. Keep its task IDs and evidence in TodoWrite as you diagnose, repair, deploy and verify. Create a new checklist only when the saved board is explicitly absent. Mark a task complete only when its result is observed; a successful tool request is not proof of a successful merge, deployment or verification. Publish concise progress explanations when the stage changes or a blocker is found. TodoWrite records progress only; all repository and deployment actions still use the brokered tools.\n\nUse actions_wait with a run ID for queued or running deployments, and repair_wait with a PR number for pending checks. Leave their default five-minute bound: each tool reads through the gateway every 30 seconds and returns when status changes or the bound expires, without extra model turns between unchanged observations.\n\nAssigned story: ${context.title} (#${context.issue}). Allowed workflows: ${context.workflows.join(', ') || 'none'}. Use the repair and Actions tools to fix, test, merge and retry until runtime verification passes. Return a short report of observed runs and remaining unverified evidence.`;
}

export async function main(): Promise<void> {
  if (process.env.ADP_ENGINE_OPERATIONS_STORY !== 'true' || process.env.AGENT_TYPE !== 'operations') {
    throw new Error('Operations story worker not admitted');
  }
  const context = JSON.parse(process.env.ADP_OPERATIONS_STORY_CONTEXT || '{}');
  const prompt = storyPrompt({ ...context, issue: Number(process.env.ISSUE_NUMBER) });
  let observeBoard: ((operation: string, text: string) => void) | undefined;
  const tools = operationsStoryTools((operation, text) => observeBoard?.(operation, text));
  if (!process.env.ADP_OPERATIONS_REPORT_FILE) throw new Error('Operations report destination missing');
  let report = '';
  let outcome = '';
  let lastAssistantText = '';
  let failure = '';
  try {
    for await (const message of operationsQuery({ prompt, options: {
      model: process.env.ANTHROPIC_MODEL,
      cwd: process.env.WORK_DIR,
      includePartialMessages: true,
      tools: ['TodoWrite'],
      allowedTools: ['TodoWrite', ...tools.allowedTools],
      // The pinned SDK defaults to TaskCreate/TaskUpdate, which disables
      // TodoWrite even when explicitly requested. Keep the shared checklist
      // protocol used by ClaudeProgress available for this bounded worker.
      env: { ...process.env, CLAUDE_CODE_ENABLE_TASKS: 'false', CLAUDE_CODE_ENABLE_TODO_TOOLS: 'true' },
      mcpServers: tools.mcpServers,
      spawnClaudeCodeProcess: spawnSdkWithoutAppKey,
      permissionMode: 'bypassPermissions',
      maxTurns: operationsMaxTurns(process.env.ADP_OPERATIONS_MAX_TURNS),
      settingSources: [],
    } }, board => { observeBoard = board.observeToolResult; })) {
      if (message.type === 'assistant') {
        const text = extractAssistantText(message);
        if (text) lastAssistantText = text;
      }
      if (message.type === 'result') {
        outcome = message.subtype;
        if (message.subtype === 'success') report = message.result;
        else failure = sdkFailure((message as { errors?: string[] }).errors?.join('\n') || message.subtype);
        // Result is terminal. Waiting for another SDK message can hang or
        // turn an already-observed result into a transport/cleanup failure.
        break;
      }
    }
  } catch (error) {
    // The SDK can throw instead of yielding an error result (including its
    // turn limit). Always leave a receipt for the engine's next attempt.
    failure = isControlCancellation(error) ? 'control_cancelled' : sdkFailure(error instanceof Error ? error.message : String(error));
    if (!outcome) outcome = failure === 'control_cancelled' ? 'error_cancelled' : failure === 'turn_limit' ? 'error_max_turns' : 'error_sdk';
    console.error(`Operations SDK diagnostic: ${failure}`);
  }
  if (outcome !== 'success') {
    // The engine's operations report receipt must never be empty: record the
    // bounded reason (turn cap, budget, error) and the agent's last words so the
    // retry and the operator can see what the story attempted.
    const summary = `Operations story ended without a success result (${outcome || 'no result'}). Diagnostic: ${failure || 'no_terminal_result'}. Last assistant text: ${lastAssistantText}`;
    writeFileSync(process.env.ADP_OPERATIONS_REPORT_FILE, JSON.stringify({ report: bounded(summary) }), { mode: 0o600 });
    console.error(`Operations story result subtype: ${outcome || 'none'}`);
    throw new Error('Operations story did not complete');
  }
  if (!report) {
    // A success result with no final text still happened: say so instead of
    // discarding the whole run as "unknown".
    report = `Operations story ended with a success result but no final text. Last assistant text: ${lastAssistantText}`;
  }
  const size = Buffer.byteLength(report);
  if (size > REPORT_LIMIT_BYTES) console.error(`Operations report truncated from ${size} to ${REPORT_LIMIT_BYTES} bytes`);
  writeFileSync(process.env.ADP_OPERATIONS_REPORT_FILE, JSON.stringify({ report: bounded(report) }), { mode: 0o600 });
}

const REPORT_LIMIT_BYTES = 12000;
// The persona loop (diagnose → fix → PR → CI → merge → deploy → verify) needs
// far more than the 20 turns the first cut allowed; every live operate story
// exhausted them on workspace reads and failed without a report.
const DEFAULT_MAX_TURNS = 60;

// Keep failure causes useful without copying provider bodies, credentials,
// URLs or arbitrary SDK stderr into the durable agent report.
export function sdkFailure(message: string): string {
  if (/maximum number of turns|max.?turns/i.test(message)) return 'turn_limit';
  if (/context.{0,30}(large|long|limit)|prompt.{0,20}too long/i.test(message)) return 'context_limit';
  if (/budget|spend limit/i.test(message)) return 'budget_limit';
  if (/timed?\s*out|timeout/i.test(message)) return 'sdk_timeout';
  if (/connection|socket|ECONN|network|transport interrupted/i.test(message)) return 'sdk_connection_interrupted';
  const status = message.match(/(?:HTTP\s*|status(?: code)?[=: ]+)([45][0-9]{2})\b/i);
  if (status) return `sdk_http_${status[1]}`;
  const exit = message.match(/(?:exited with code|exit code)\s*([0-9]{1,3})\b/i);
  if (exit) return `sdk_process_exit_${exit[1]}`;
  return 'sdk_unclassified_error';
}

export function operationsMaxTurns(value: string | undefined): number {
  if (value === undefined || value === '') return DEFAULT_MAX_TURNS;
  if (!/^[1-9][0-9]{0,2}$/.test(value)) throw new Error('Invalid ADP_OPERATIONS_MAX_TURNS');
  return Number(value);
}

export function bounded(text: string): string {
  return Buffer.byteLength(text) <= REPORT_LIMIT_BYTES ? text : Buffer.from(text).subarray(0, REPORT_LIMIT_BYTES - 3).toString() + '...';
}

function extractAssistantText(message: { message?: { content?: unknown } }): string {
  const content = message.message?.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((block): block is { type: string; text: string } => typeof block === 'object' && block !== null && (block as { type?: string }).type === 'text' && typeof (block as { text?: unknown }).text === 'string')
    .map(block => block.text)
    .join('\n');
}

if (require.main === module) main().catch((error: unknown) => {
  // Name the cause: a fixed line hid whether the SDK threw, the result was not a
  // success, or the report could not be written.
  const reason = error instanceof Error ? error.message : String(error);
  console.error(`Operations story did not complete with verified tools and a bounded report: ${reason.slice(0, 500)}`);
  process.exitCode = 1;
});
