/** Read the platform-managed ConfigMap projection on every verification. */
import { closeSync, fstatSync, openSync, readSync } from 'fs';
import { type KeyObject } from 'crypto';
import { parseVerificationKeys } from './control-envelope';

export function readControlKeyring(path: string): Map<string, KeyObject> {
  let fd: number | undefined;
  try {
    fd = openSync(path, 'r');
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size > 16_384) return new Map();
    const buffer = Buffer.alloc(16_385);
    const length = readSync(fd, buffer, 0, buffer.length, 0);
    if (length > 16_384) return new Map();
    return parseVerificationKeys(buffer.subarray(0, length).toString('utf8'));
  } catch {
    // An unavailable projection must not restore keys that were retired.
    return new Map();
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}
