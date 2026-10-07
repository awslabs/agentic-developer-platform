/** Bounded, invocation-local authored explanations. No SDK objects or tool inputs. */
import { truncateUtf8 } from './reporting-text';
import { containsSecret } from './experience-save-hook';

/** Provider-neutral identity for replaceable public messages and tool lifecycle. */
export interface ProgressDetail {
  id: string;
  category: 'message' | 'tool' | 'plan';
  state: 'running' | 'completed' | 'failed';
  started_at?: string;
  plan_scope?: 'assignment' | 'inspection';
}
export interface ExplanationEvent {
  version: 1;
  invocation_id: string;
  generation: number;
  sequence: number;
  timestamp: string;
  kind: 'explanation' | 'terminal';
  payload: { text?: string; progress?: ProgressDetail };
}
export const EVENT_BYTES = 16 * 1024;
export const HISTORY_BYTES = 256 * 1024;
export const HISTORY_EVENTS = 128;
export const MAX_SUBSCRIBERS = 4;
export function explanationsEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.FEATURE_AGENT_EXPLANATIONS_ENABLED === 'true';
}
export class ExplanationEvents {
  private history: Array<{ event: ExplanationEvent; bytes: number }> = [];
  private bytes = 0;
  private sequence = 0;
  private latestPlan?: ExplanationEvent;
  private ended = false;
  private progressItems = new Map<string, { text: string; state: string; time: number; started: string }>();
  private subscribers = new Set<(event: ExplanationEvent) => void>();
  constructor(readonly invocationId: string, readonly generation: number) {}
  cursor(sequence: number): string { return `${this.invocationId}:${this.generation}:${sequence}`; }
  publish(text: string, progress?: ProgressDetail): void {
    if (!text.trim() || this.ended) return;
    if (progress?.category === 'plan' && progress.plan_scope === 'inspection') {
      progress = { ...progress, category: 'message' };
      text = 'Inspection steps (not the assignment checklist)\n\n' + text;
    }
    if (containsSecret(text) || Object.entries(process.env).some(([key, value]) =>
      /TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY/.test(key) && value && value.length >= 8 && text.includes(value))) {
      text = '[Explanation omitted because it contains credential-like content.]';
    }
    if (progress) {
      const previous = this.progressItems.get(progress.id);
      const now = Date.now();
      if (previous && previous.state === progress.state && (previous.text === text ||
          (progress.category === 'message' && progress.state === 'running' && now - previous.time < 250))) return;
      const started = previous?.started ?? new Date(now).toISOString();
      this.progressItems.set(progress.id, { text, state: progress.state, time: now, started });
      if (this.progressItems.size > HISTORY_EVENTS) this.progressItems.delete(this.progressItems.keys().next().value!);
      progress = { ...progress, started_at: started };
    }
    this.append('explanation', { text: truncateUtf8(text, EVENT_BYTES / 2, '\n[Live preview truncated; see final transcript.]'), ...(progress ? { progress: { id: progress.id.slice(0, 256), category: progress.category, state: progress.state, started_at: progress.started_at } } : {}) });
  }
  finish(): void {
    if (this.ended) return;
    this.ended = true;
    this.append('terminal', {});
  }
  private append(kind: ExplanationEvent['kind'], payload: ExplanationEvent['payload']): void {
    const event: ExplanationEvent = { version: 1, invocation_id: this.invocationId,
      generation: this.generation, sequence: ++this.sequence, timestamp: new Date().toISOString(), kind, payload };
    if (Buffer.byteLength(JSON.stringify(event)) > EVENT_BYTES && payload.text) {
      event.payload = { ...payload, text: truncateUtf8(payload.text, 2000, '\n[Live preview truncated; see final transcript.]') };
    }
    if (event.payload.progress?.category === 'plan') this.latestPlan = event;
    const bytes = Buffer.byteLength(JSON.stringify(event));
    this.history.push({ event, bytes }); this.bytes += bytes;
    while (this.bytes > HISTORY_BYTES - (this.latestPlan ? EVENT_BYTES : 0) || this.history.length > HISTORY_EVENTS - (this.latestPlan ? 1 : 0)) this.bytes -= this.history.shift()!.bytes;
    for (const send of this.subscribers) {
      try { send(event); } catch { this.subscribers.delete(send); }
    }
  }
  replay(cursor?: string): { reset: boolean; events: ExplanationEvent[] } {
    const prefix = `${this.invocationId}:${this.generation}:`;
    const raw = cursor?.startsWith(prefix) ? cursor.slice(prefix.length) : '';
    const n = /^\d+$/.test(raw) ? Number(raw) : -1;
    const first = this.history[0]?.event.sequence ?? 1;
    const reset = !!cursor && (!Number.isSafeInteger(n) || n < first - 1 || n > this.sequence);
    const events = this.history.filter(x => reset || !cursor || x.event.sequence > n).map(x => x.event);
    if ((reset || !cursor) && this.latestPlan && !events.includes(this.latestPlan)) events.unshift(this.latestPlan);
    return { reset: reset || (!cursor && first > 1), events };
  }
  subscribe(send: (event: ExplanationEvent) => void): () => void {
    if (this.subscribers.size >= MAX_SUBSCRIBERS) throw new Error('too many subscribers');
    this.subscribers.add(send);
    return () => { this.subscribers.delete(send); };
  }
}
