/** Existing GitHub/Activity reporting, with a report completion contract. */
import type { ProgressDetail } from './explanation-events';
import { readFileSync, writeFileSync } from 'node:fs';
import { LiveStatusComment, createWorkerStages } from './github-comments';
import { CheckRunStreamer } from './components/checkRunStreamer';
import { createWorkerActivityLog } from './worker-activity-log';
import { publicDeveloperText } from './codex-developer-reporting';
import { writeFailureReport } from './failure-report';

function metadata(fields: Record<string, unknown>) {
  const path = '/tmp/adp-result-metadata.json';
  let previous = {};
  try { previous = JSON.parse(readFileSync(path, 'utf8')); } catch { /* first event */ }
  writeFileSync(path, JSON.stringify({ ...previous, ...fields }), { mode: 0o600 });
}

export async function createCodexPersonaReporter(context: { repository: string; issue: number; persona: string; model: string }) {
  if (!process.env.ADP_MESSAGE_ID) throw new Error('Missing invocation');
  const token = () => process.env.GH_APP_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN || '';
  const activity = createWorkerActivityLog(context.persona, String(context.issue));
  await activity.start();
  const log = (level: string, text: string) => activity.log(level, publicDeveloperText(text), { invocation_id: process.env.ADP_MESSAGE_ID });
  const timer = setInterval(() => { void activity.flush(); }, 5000);
  timer.unref();
  const [owner, repo] = context.repository.split('/');
  const live = new LiveStatusComment(createWorkerStages(context.persona.replace(/^agent-codex-/, '')), {
    owner, repo, issueNumber: context.issue, token: token(), log,
  });
  const check = new CheckRunStreamer({ checkRunId: Number(process.env.CHECK_RUN_ID || 0), repo: context.repository,
    tokenProvider: token, persona: context.persona, issueNumber: context.issue, model: context.model,
    costLabel: 'See Agent Activity for metered usage', log: text => log('WARN', text) });
  let turns = 0;
  let ended = false;
  const close = async () => { clearInterval(timer); check.destroy(); await activity.flush(); };
  try {
    await live.post();
    metadata({ outcome_comment_url: live.getCommentUrl(), session_completed: false, codex_persona_report: false });
    live.transition(0, 'complete', 'Worker and admitted persona ready');
    live.transition(1, 'in_progress', 'Processing the requested task');
    await live.flush();
  } catch (error) { await close(); throw error; }
  return {
    log,
    progress(text: string, detail?: ProgressDetail) {
      text = publicDeveloperText(text);
      if (detail?.category === 'tool') live.appendActivity(text);
      else live.setExplanation(text);
      check.onTurn({ turn: ++turns, content: [{ type: 'text', text }] });
      log('INFO', text);
    },
    async finish(result: { response: string; threadId: string; usage: unknown }, provenance: unknown) {
      if (ended) throw new Error('Report already finalized');
      const summary = publicDeveloperText(result.response);
      try {
        live.transition(1, 'complete', 'Report produced');
        await live.finalizeSuccess({ details: summary });
        metadata({ session_completed: true, session_id: result.threadId, usage: result.usage, num_turns: turns,
          codex_persona_report: true, codex_persona_invocation: process.env.ADP_MESSAGE_ID,
          codex_persona_provenance: provenance, outcome_comment_url: live.getCommentUrl() });
        check.onResult({ turns });
        ended = true;
      } finally { await close(); }
    },
    async fail(error: unknown) {
      if (ended) return;
      ended = true;
      try {
        writeFailureReport(error);
        metadata({ session_completed: false });
        await live.finalizeFailure({ error: publicDeveloperText(error instanceof Error ? error.message : String(error)), durationMs: live.getDurationMs() });
      } finally { await close(); }
    },
  };
}
