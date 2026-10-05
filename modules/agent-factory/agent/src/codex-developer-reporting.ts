/** Native Codex events feed the same user-facing sinks as agent-worker.ts. */
import type { ProgressDetail } from './explanation-events';
import { readFileSync, writeFileSync } from 'node:fs';
import { LiveStatusComment, createWorkerStages } from './github-comments';
import { CheckRunStreamer } from './components/checkRunStreamer';
import { CodexControlAdapter } from './harnesses/codex-control';
import { startControlRuntime } from './control-runtime-factory';
import { containsSecret } from './experience-save-hook';
import { writeFailureReport } from './failure-report';
import { createWorkerActivityLog } from './worker-activity-log';

export interface DeveloperReportingContext { repository: string; issue: number; model: string; persona?: string }
export interface DeveloperReporter {
  control?: { signal: AbortSignal; socket: string; operation<T>(work: () => Promise<T>): Promise<T> };
  observeEvent?(event: { type: string; item?: { id: string; type: string } }): void;
  progress?(text: string, detail: ProgressDetail): void;
  explanation(text: string): void;
  activity(text: string): void;
  session(id: string): void;
  finish(result: { summary: string; prUrl?: string; usage?: unknown }): Promise<void>;
  fail(error: unknown): Promise<void>;
}

export function publicDeveloperText(text: string): string {
  for (const [key, value] of Object.entries(process.env)) {
    if (/TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY|API_KEY/.test(key) && value && value.length >= 8) {
      text = text.split(value).join('[redacted]');
    }
  }
  return containsSecret(text) ? '[Activity omitted because it contains credential-like content.]' : text;
}

function metadata(fields: Record<string, unknown>): void {
  const path = '/tmp/adp-result-metadata.json';
  let previous = {};
  try { previous = JSON.parse(readFileSync(path, 'utf8')); } catch { /* first event */ }
  writeFileSync(path, JSON.stringify({ ...previous, ...fields }), { mode: 0o600 });
}

export async function createCodexDeveloperReporter(context: DeveloperReportingContext): Promise<DeveloperReporter> {
  if (!process.env.ADP_MESSAGE_ID) throw new Error('Codex developer reporting requires a dispatched ADP invocation');
  const persona = context.persona ?? 'agent-codex-developer';
  const reviewer = persona === 'agent-codex-reviewer';
  const architect = persona === 'agent-codex-architect';
  const token = () => process.env.GH_APP_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN || '';
  const activityLog = createWorkerActivityLog(persona, String(context.issue));
  await activityLog.start();
  const log = (level: string, message: string) => activityLog.log(level, publicDeveloperText(message), {
    invocation_id: process.env.ADP_MESSAGE_ID,
  });
  const logTimer = setInterval(() => { void activityLog.flush(); }, 5000);
  logTimer.unref?.();
  const [owner, repo] = context.repository.split('/');
  const live = new LiveStatusComment(createWorkerStages(reviewer ? 'reviewer' : architect ? 'architect' : 'developer'), {
    owner, repo, issueNumber: context.issue, token: token(), log,
  });
  const check = new CheckRunStreamer({
    checkRunId: Number(process.env.CHECK_RUN_ID || 0), repo: context.repository,
    tokenProvider: token, persona, issueNumber: context.issue,
    model: context.model, costLabel: 'See Agent Activity for metered usage', log: message => log('WARN', message),
  });
  const control = await startControlRuntime({ log, createAdapter: gate => new CodexControlAdapter(gate, { sdkCommands: true }) });
  const adapter = control.runtime?.adapter;
  if (adapter) {
    adapter.drainSteering = () => control.runtime!.steerQueue.flush();
    await adapter.start();
  }
  let sequence = 0;
  let ended = false;
  const explanation = (text: string) => {
    text = publicDeveloperText(text);
    live.setExplanation(text);
    control.events?.publish(text);
    check.onTurn({ turn: ++sequence, content: [{ type: 'text', text }] });
    log('INFO', text);
  };
  const cleanup = async () => {
    clearInterval(logTimer);
    await activityLog.flush();
    control.events?.finish();
    control.runtime?.steerQueue.dispose('Codex run ended');
    await adapter?.dispose();
    check.destroy();
    await control.listener?.stop();
  };
  try {
    await live.post();
    metadata({ outcome_comment_url: live.getCommentUrl() });
    live.transition(0, 'complete', 'Repository and worker ready');
    live.transition(1, 'in_progress', reviewer ? 'Reviewing, fixing and testing the change' : architect ? 'Auditing the repository and preparing the design PR' : 'Reading the issue and developing the change');
    explanation(`Working on ${context.repository}#${context.issue} with the Codex SDK. Progress and command activity will update here.`);
    await live.flush();
  } catch (error) {
    await live.finalizeFailure({ error: publicDeveloperText(String(error)), durationMs: live.getDurationMs() }).catch(() => {});
    await cleanup();
    throw error;
  }
  return {
    ...(adapter ? { control: { signal: adapter.signal, socket: adapter.socket,
      async operation<T>(work: () => Promise<T>): Promise<T> {
        adapter.signal.throwIfAborted();
        const admission = await control.runtime!.gate.admit('Codex controller operation', adapter.signal);
        if (admission.decision !== 'admit') throw new Error('Codex controller operation cancelled');
        try { adapter.signal.throwIfAborted(); return await work(); }
        finally { control.runtime!.gate.settle(admission.ticket); }
      } } } : {}),
    observeEvent(event) { adapter?.observeSdkEvent(event); },
    explanation,
    progress(text, detail) {
      text = publicDeveloperText(text);
      control.events?.publish(text, detail);
      if (detail.state !== 'running' || detail.category === 'tool' || detail.category === 'plan') {
        if (detail.category === 'plan') live.setTaskChecklist(text);
        else if (detail.category === 'message') live.setExplanation(text);
        else live.appendActivity(text);
        check.onTurn({ turn: ++sequence, content: [{ type: 'text', text }] });
        log('INFO', text);
      }
    },
    activity(text) {
      text = publicDeveloperText(text);
      live.appendActivity(text);
      control.events?.publish(text);
      check.onTurn({ turn: ++sequence, content: [{ type: 'tool_use', name: 'Codex', input: { command: text } }] });
      log('INFO', text);
    },
    session(id) { metadata({ session_id: id }); },
    async finish(result) {
      if (ended) return;
      try {
        explanation(result.summary);
        metadata({ session_completed: true, usage: result.usage, num_turns: sequence, pr_url: result.prUrl });
        live.transition(1, 'complete', reviewer ? 'Review completed' : 'Pull request published');
        await live.finalizeSuccess({ details: publicDeveloperText(result.summary), prUrl: result.prUrl,
          artifacts: Number(process.env.CHECK_RUN_ID) > 0
            ? [`[Full activity stream](https://github.com/${context.repository}/runs/${process.env.CHECK_RUN_ID})`] : [],
        });
        check.onResult({ turns: sequence });
        ended = true;
      } finally { await cleanup(); }
    },
    async fail(error) {
      if (ended) return; ended = true;
      try {
        writeFailureReport(error);
        const text = publicDeveloperText(error instanceof Error ? error.message : String(error));
        explanation(`Run failed: ${text}`);
        metadata({ session_completed: false });
        await live.finalizeFailure({ error: text, durationMs: live.getDurationMs() });
      } finally { await cleanup(); }
    },
  };
}
