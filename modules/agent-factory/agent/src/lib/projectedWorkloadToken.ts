import { constants, openSync, fstatSync, readSync, closeSync } from 'node:fs';

export function readIdentityToken(path: string | undefined): string {
  if (!path) throw new Error('identity unavailable');
  const fd = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    if (!fstatSync(fd).isFile()) throw new Error('identity unavailable');
    const bytes = Buffer.alloc(16387);
    const count = readSync(fd, bytes, 0, bytes.length, null);
    const token = bytes.subarray(0, count).toString('utf8').replace(/\r?\n$/, '');
    if (!token || token.length > 16384 || !/^[\x21-\x7e]+$/.test(token)) throw new Error('identity unavailable');
    return token;
  } finally { closeSync(fd); }
}
