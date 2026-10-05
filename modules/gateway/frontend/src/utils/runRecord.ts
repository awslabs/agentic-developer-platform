/** Versioned, untrusted metadata embedded in the existing authorized transcript. */
export interface RecordedTask { id: string; text: string; status: 'pending' | 'in_progress' | 'completed' }
interface Snapshot { at: string; tasks: RecordedTask[] }
export interface ClosureReport {
  summary: string; completed: string[]; remaining: string[]; delivery: string;
  reviewed_revision?: string; reporting_notes: string[];
}
export interface RunRecord {
  version: 1; invocation_id: string; persona: string; model: string; repository: string; issue: number;
  started_at: string; captured_at: string; capture_closed_at?: string;
  worker?: string; region?: string; starting_revision?: string; saved_revision?: string;
  session_ids: string[]; first_checklist?: Snapshot; latest_checklist?: Snapshot;
  task_transitions: { at: string; id: string; from: string; to: string }[];
  history_truncated: boolean; evidence: { at: string; text: string }[];
  closure_report?: ClosureReport;
}
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v: unknown, max = 4096): v is string => typeof v === 'string' && v.length <= max;
const date = (v: unknown): v is string => text(v, 64) && Number.isFinite(Date.parse(v));
const states = ['pending', 'in_progress', 'completed'];
function snapshot(v: unknown): boolean {
  return v === undefined || (object(v) && date(v.at) && Array.isArray(v.tasks) && v.tasks.length <= 100 &&
    new Set(v.tasks.map(t => object(t) ? t.id : null)).size === v.tasks.length &&
    v.tasks.every(t => object(t) && text(t.id, 24) && /^[a-f0-9]{24}$/.test(t.id) && text(t.text, 1024) && !!t.text && states.includes(String(t.status))));
}
export function parseRunRecord(markdown: string, invocationId: string): RunRecord | undefined {
  // Only the leading envelope is authoritative; never parse a tool's quoted examples.
  const match = /^<!-- adp-run-record:v1 ([A-Za-z0-9+/=]{1,1400000}) -->/.exec(markdown);
  if (!match) return;
  try {
    const binary = atob(match[1]);
    const value: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(binary, c => c.charCodeAt(0))));
    if (!object(value) || value.version !== 1 || value.invocation_id !== invocationId || !invocationId) return;
    if (!['persona', 'model', 'repository'].every(k => text(value[k])) || !Number.isSafeInteger(value.issue)) return;
    if (!date(value.started_at) || !date(value.captured_at) || (value.capture_closed_at !== undefined && !date(value.capture_closed_at))) return;
    if (!['worker', 'region', 'starting_revision', 'saved_revision'].every(k => value[k] === undefined || text(value[k], 512))) return;
    if (!snapshot(value.first_checklist) || !snapshot(value.latest_checklist)) return;
    if (!Array.isArray(value.session_ids) || value.session_ids.length > 32 || !value.session_ids.every(s => text(s, 256))) return;
    if (!Array.isArray(value.task_transitions) || value.task_transitions.length > 1000 || !value.task_transitions.every(t => object(t) && date(t.at) && text(t.id, 24) && [...states, 'not_listed'].includes(String(t.from)) && [...states, 'not_listed'].includes(String(t.to)))) return;
    if (typeof value.history_truncated !== 'boolean' || !Array.isArray(value.evidence) || value.evidence.length > 32 || !value.evidence.every(e => object(e) && date(e.at) && text(e.text, 8192))) return;
    if (value.closure_report !== undefined) {
      const c = value.closure_report;
      const list = (a: unknown) => Array.isArray(a) && a.length <= 100 && a.every(s => text(s, 4096));
      if (!object(c) || !text(c.summary, 8192) || !text(c.delivery, 1024)
          || !list(c.completed) || !list(c.remaining) || !list(c.reporting_notes)
          || (c.reviewed_revision !== undefined && !(text(c.reviewed_revision, 40) && /^[a-f0-9]{40}$/.test(c.reviewed_revision)))) {
        delete value.closure_report; // A bad optional report must not hide the transcript/checklist.
      }
    }
    return value as unknown as RunRecord;
  } catch { return; }
}
export function checklistContribution(record: RunRecord) {
  const first = record.first_checklist?.tasks ?? [];
  const latest = record.latest_checklist?.tasks ?? [];
  return {
    completedSinceFirst: latest.filter(t => t.status === 'completed' && first.some(f => f.id === t.id && f.status !== 'completed')),
    alreadyComplete: first.filter(t => t.status === 'completed'),
    addedComplete: latest.filter(t => t.status === 'completed' && !first.some(f => f.id === t.id)),
    remaining: latest.filter(t => t.status !== 'completed'),
    removed: first.filter(t => !latest.some(l => l.id === t.id)),
  };
}
