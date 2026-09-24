/** Bounded, invocation-local authored explanations. No SDK objects or tool inputs. */
import { truncateUtf8 } from './reporting-text';
import { containsSecret } from './experience-save-hook';

export interface ExplanationEvent {
  version: 1;
  invocation_id: string;
  generation: number;
  sequence: number;
  timestamp: string;
  kind: 'explanation' | 'terminal';
  payload: { text?: string };
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
  private ended = false;
  private subscribers = new Set<(event: ExplanationEvent) => void>();
  constructor(readonly invocationId: string, readonly generation: number) {}
  cursor(sequence: number): string { return `${this.invocationId}:${this.generation}:${sequence}`; }
  publish(text: string): void {
    if (!text.trim() || this.ended) return;
    if (containsSecret(text) || Object.entries(process.env).some(([key, value]) =>
      /TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY/.test(key) && value && value.length >= 8 && text.includes(value))) {
      text = '[Explanation omitted because it contains credential-like content.]';
    }
    this.append('explanation', { text: truncateUtf8(text, EVENT_BYTES / 2, '\n[Live preview truncated; see final transcript.]') });
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
      event.payload = { text: truncateUtf8(payload.text, 2000, '\n[Live preview truncated; see final transcript.]') };
    }
    const bytes = Buffer.byteLength(JSON.stringify(event));
    this.history.push({ event, bytes }); this.bytes += bytes;
    while (this.bytes > HISTORY_BYTES || this.history.length > HISTORY_EVENTS) this.bytes -= this.history.shift()!.bytes;
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
    return { reset: reset || (!cursor && first > 1), events: this.history.filter(x => reset || !cursor || x.event.sequence > n).map(x => x.event) };
  }
  subscribe(send: (event: ExplanationEvent) => void): () => void {
    if (this.subscribers.size >= MAX_SUBSCRIBERS) throw new Error('too many subscribers');
    this.subscribers.add(send);
    return () => { this.subscribers.delete(send); };
  }
}
