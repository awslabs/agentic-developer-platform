/** Reload the worker supervisor's credential lease without restarting its journal. */
import { closeSync, fstatSync, openSync, readSync } from 'fs';

export interface ListenerToken {
  token: string;
  expiresAt: number;
}

export class ControlCredentials {
  private epoch = 0;

  constructor(private readonly path: string, private readonly runId: string, private readonly generation: number) {}

  read(now = Date.now()): ListenerToken[] {
    let fd: number | undefined;
    try {
      fd = openSync(this.path, 'r');
      const stat = fstatSync(fd);
      if (!stat.isFile() || stat.size > 8192 || (stat.mode & 0o077) !== 0) return [];
      const bytes = Buffer.alloc(8193);
      const length = readSync(fd, bytes, 0, bytes.length, 0);
      if (length > 8192) return [];
      const doc = JSON.parse(bytes.subarray(0, length).toString('utf8'));
      if (doc.version !== 1 || doc.run_id !== this.runId || doc.generation !== this.generation) return [];
      const current = doc.current;
      if (!current || !Number.isSafeInteger(current.epoch) || current.epoch < 1 || current.epoch < this.epoch) return [];
      const parse = (value: any): ListenerToken | null => {
        if (!value || typeof value.token !== 'string' || !/^[A-Za-z0-9_-]{32,256}$/.test(value.token)) return null;
        if (typeof value.expires_at !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value.expires_at)) return null;
        const expiresAt = Date.parse(value.expires_at);
        return Number.isFinite(expiresAt) && expiresAt > now ? { token: value.token, expiresAt } : null;
      };
      const active = parse(current);
      if (!active) return [];
      this.epoch = current.epoch;
      const accepted = [active];
      if (doc.previous && doc.previous.epoch === current.epoch - 1) {
        const previous = parse(doc.previous);
        const staged = Date.parse(doc.staged_at);
        const until = Date.parse(doc.previous.valid_until);
        if (previous && Number.isFinite(staged) && staged <= now && until <= staged + 30_000 && now < until) {
          accepted.push({ token: previous.token, expiresAt: Math.min(previous.expiresAt, until) });
        }
      }
      return accepted;
    } catch {
      // Missing/malformed/expired files never restore the startup credential.
      return [];
    } finally {
      if (fd !== undefined) closeSync(fd);
    }
  }
}
