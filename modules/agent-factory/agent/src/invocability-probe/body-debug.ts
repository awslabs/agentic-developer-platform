import { canonicalJson, normalizeRequestShape } from './canonical-json';

/**
 * Operator debug switch (#7993). When set to "1", the manifest generator prints
 * every captured body and the probe runner prints the body of a request that
 * failed the shape check. Off by default: a passing probe never logs its body.
 * The body is the SDK's prompts, tool schemas and model parameters; SigV4
 * credentials travel in headers and are never part of it.
 */
export const PROBE_PRINT_BODY_ENV = 'ADP_PROBE_PRINT_BODY';

/**
 * Container runtimes split log lines around 16 KiB and the reassembly is not
 * reliable for a 50 KiB body, so the canonical (normalized, key-sorted) body is
 * written as numbered chunks `[<tag>:<n>/<total>] <chunk>`. Reassemble by
 * selecting the tag's lines in order, stripping the prefix up to "] " and
 * joining them without newlines (see task-probes.md for the shell one-liner).
 */
export const PROBE_BODY_CHUNK_CHARS = 4000;

export function probeBodyPrintEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env[PROBE_PRINT_BODY_ENV] === '1';
}

/** Print the exact text that requestShapeSha256 digests, in log-safe chunks. */
export function printCanonicalProbeBody(
  tag: string,
  body: unknown,
  write: (line: string) => void = (line) => console.error(line),
): void {
  const text = canonicalJson(normalizeRequestShape(body));
  const chunks = Math.max(1, Math.ceil(text.length / PROBE_BODY_CHUNK_CHARS));
  for (let index = 0; index < chunks; index++) {
    write(`[${tag}:${index + 1}/${chunks}] ${text.slice(index * PROBE_BODY_CHUNK_CHARS, (index + 1) * PROBE_BODY_CHUNK_CHARS)}`);
  }
}
