/** Per-invocation, agent-reported progress. Never an acceptance or billing receipt. */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { writeFileSync, renameSync } from 'node:fs';
import { containsSecret } from './experience-save-hook';
import { truncateUtf8 } from './reporting-text';

export interface RecordedTask { id: string; text: string; status: 'pending' | 'in_progress' | 'completed' }
export interface ChecklistSnapshot { at: string; tasks: RecordedTask[] }
export interface RunRecord {
  version: 1; invocation_id: string; persona: string; model: string; repository: string; issue: number;
  started_at: string; captured_at: string; capture_closed_at?: string;
  worker?: string; region?: string; starting_revision?: string; saved_revision?: string;
  session_ids: string[]; first_checklist?: ChecklistSnapshot; latest_checklist?: ChecklistSnapshot;
  task_transitions: Array<{ at: string; id: string; from: string; to: string }>;
  history_truncated: boolean;
  evidence: Array<{ at: string; text: string }>;
}
export function recordText(value: string, limit = 4096): string {
  let text = value;
  for (const [key, secret] of Object.entries(process.env)) {
    if (/TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY|API_KEY/.test(key) && secret && secret.length >= 8) {
      text = text.split(secret).join('[redacted]');
    }
  }
  return containsSecret(text) ? '[Credential-like content omitted.]' : truncateUtf8(text, limit, '…');
}
/** Parse only our runtime's checklist format; prose and tool output are not task state. */
export function parseTaskChecklist(text: string): RecordedTask[] | undefined {
  const tasks: RecordedTask[] = [];
  for (const line of text.split('\n')) {
    const match = /^- ([☑☐]) (.+)$/.exec(line.trim());
    if (!match) continue;
    const inProgress = match[2].endsWith(' (in progress)');
    const label = (inProgress ? match[2].slice(0, -14) : match[2]).trim();
    if (!label || tasks.length >= 100 || label.length > 1024) return;
    const safe = recordText(label, 4096);
    if (safe !== label) return; // Redaction must not fabricate a different task identity.
    const id = createHash('sha256').update(label).digest('hex').slice(0, 24);
    if (tasks.some(task => task.id === id)) return;
    tasks.push({ id, text: label, status: match[1] === '☑' ? 'completed' : inProgress ? 'in_progress' : 'pending' });
  }
  return tasks.length ? tasks : undefined;
}
function revision(): string | undefined {
  try {
    const head = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8', timeout: 1500, stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    return /^[a-f0-9]{40}$/.test(head) ? head : undefined;
  } catch { return undefined; }
}
export class RunRecordCapture {
  readonly record: RunRecord;
  constructor(context: { persona: string; model: string; repo: string; issueNumber: number },
    private readonly path = '/tmp/adp-run-record.json', private readonly now = () => new Date().toISOString()) {
    this.record = {
      version: 1, invocation_id: recordText(process.env.ADP_MESSAGE_ID ?? '', 512),
      persona: recordText(context.persona), model: recordText(context.model), repository: recordText(context.repo),
      issue: context.issueNumber, started_at: now(), captured_at: now(), session_ids: [],
      worker: recordText(process.env.JOB_NAME || process.env.HOSTNAME || '', 512),
      region: recordText(process.env.AWS_REGION ?? '', 128), starting_revision: revision(),
      task_transitions: [], history_truncated: false, evidence: [],
    };
    this.persist();
  }
  checklist(text: string): void {
    if (this.record.capture_closed_at) return;
    const tasks = parseTaskChecklist(text);
    if (!tasks) return;
    const prior = this.record.latest_checklist;
    if (JSON.stringify(prior?.tasks) === JSON.stringify(tasks)) return;
    const at = this.now();
    this.record.first_checklist ??= { at, tasks: structuredClone(tasks) };
    if (prior) {
      for (const task of tasks) {
        const before = prior.tasks.find(item => item.id === task.id)?.status ?? 'not_listed';
        if (before !== task.status) this.record.task_transitions.push({ at, id: task.id, from: before, to: task.status });
      }
      for (const task of prior.tasks) {
        if (!tasks.some(item => item.id === task.id)) this.record.task_transitions.push({ at, id: task.id, from: task.status, to: 'not_listed' });
      }
    }
    if (this.record.task_transitions.length > 1000) {
      this.record.history_truncated = true;
      this.record.task_transitions = this.record.task_transitions.slice(-1000);
    }
    this.record.latest_checklist = { at, tasks };
    this.record.saved_revision = revision();
    this.persist();
  }
  session(id: string): void {
    if (this.record.capture_closed_at || !/^[a-zA-Z0-9:_-]{1,256}$/.test(id) || recordText(id, 256) !== id || this.record.session_ids.includes(id)) return;
    if (this.record.session_ids.length < 32) this.record.session_ids.push(id);
    this.persist();
  }
  evidence(text: string): void {
    if (this.record.capture_closed_at || !text.trim()) return;
    const safe = recordText(text);
    if (this.record.evidence.at(-1)?.text === safe) return;
    this.record.evidence.push({ at: this.now(), text: safe });
    if (this.record.evidence.length > 32) this.record.evidence.shift();
    this.persist();
  }
  close(): void {
    if (this.record.capture_closed_at) return;
    this.record.capture_closed_at = this.now();
    this.record.saved_revision = revision();
    this.persist();
  }
  private persist(): void {
    this.record.captured_at = this.now();
    try {
      writeFileSync(this.path + '.tmp', JSON.stringify(this.record), { mode: 0o600 });
      renameSync(this.path + '.tmp', this.path);
    } catch { /* Reporting must never terminate agent execution. */ }
  }
  markdown(): string {
    const r = this.record;
    const tasks = r.latest_checklist?.tasks;
    const first = r.first_checklist?.tasks;
    const lines = [
      '<!-- adp-run-record:v1 ' + Buffer.from(JSON.stringify(r), 'utf8').toString('base64') + ' -->',
      '## Run record',
      'Run ID: ' + (r.invocation_id || 'Unavailable'),
      'Persona: ' + r.persona + ' · Model: ' + r.model,
      'Repository: ' + r.repository + ' · Issue: ' + r.issue,
      'Capture started: ' + r.started_at + ' · Last observation: ' + r.captured_at,
      'Worker: ' + (r.worker || 'Unavailable') + ' · Region: ' + (r.region || 'Unavailable'),
      'Starting revision: ' + (r.starting_revision || 'Unavailable') + ' · Last observed commit: ' + (r.saved_revision || 'Unavailable'),
      'SDK sessions: ' + (r.session_ids.join(', ') || 'Not captured'),
      'This is an agent-reported record, not proof of acceptance, merge, publication or billed usage. '
        + 'The first checklist is the first observed plan, not necessarily the state at dispatch. '
        + 'Task identity uses exact wording; renamed or removed tasks are not silently counted as completed.',
      '### Checklist at last observation',
      ...(tasks ? tasks.map(t => '- [' + (t.status === 'completed' ? 'x' : ' ') + '] ' + t.text
        + (t.status === 'in_progress' ? ' (in progress)' : '')) : ['No assignment checklist was captured.']),
      '### First observed checklist',
      ...(first ? first.map(t => '- [' + (t.status === 'completed' ? 'x' : ' ') + '] ' + t.text) : ['Unavailable.']),
      '### Recent reported evidence and handoff',
      ...(r.evidence.length ? r.evidence.map(e => e.at + '\n\n' + e.text) : ['See the original transcript for captured activity.']),
      '---',
    ];
    return lines.join('\n\n');
  }
}
