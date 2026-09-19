/**
 * Spill-to-storage for oversized tool output (#4179).
 *
 * Problem: a single verbose tool result (full test suite, big file dump, noisy
 * build log) lands in the conversation and is re-sent on every subsequent turn.
 * The worker runs with `maxTurns: 10000`, so one unlucky command can dominate
 * the context budget for the rest of the run and force an early — and lossy —
 * compaction.
 *
 * Fix: intercept tool results via the SDK's `PostToolUse` hook. When the
 * serialized payload exceeds a threshold, persist the full text to durable
 * storage and return `updatedToolOutput` — a compact stand-in carrying a
 * head/tail excerpt, the byte count, and a locator the model can read back on
 * demand. The tool itself is untouched: it has already run, its real result is
 * unchanged, and only the model's *view* is rewritten.
 *
 * Two invariants matter more than the optimization itself:
 *
 *   1. **Fail open.** Any error — serializing, persisting, formatting — results
 *      in the original output passing through unmodified. A storage problem
 *      must never become an agent-run failure. This is non-negotiable: the
 *      whole feature is an optimization, and an optimization that can break a
 *      run is a net loss.
 *   2. **The locator must be actionable.** The stand-in names `Read` and points
 *      at a path `Read` can open. A stand-in that merely says "output
 *      truncated" has destroyed information the agent may need.
 *
 * Reference: docs/research/deepseek-harness-fit-assessment.md §Q4, §7
 */
import { SpillStore, SPILL_DIR_NAME } from './store';

export { SpillStore, TmpSpillStore, SPILL_DIR_NAME } from './store';

/**
 * Default spill threshold in bytes. Conservative on purpose: spilling too
 * eagerly costs the agent a round-trip for output it should have had inline,
 * which wastes turns. 20 KiB is well above ordinary tool output (a `git status`,
 * a focused `Read`, a passing test run) and well below the point where a single
 * result starts crowding out the rest of the context.
 */
export const DEFAULT_SPILL_THRESHOLD_BYTES = 20_000;

/** Bytes of the payload head kept in the stand-in. */
const HEAD_EXCERPT_BYTES = 2_000;
/**
 * Bytes of the payload tail kept in the stand-in. Larger than the head: for
 * command output the interesting part — the error, the failure summary, the
 * final count — is almost always at the end.
 */
const TAIL_EXCERPT_BYTES = 3_000;

/**
 * Line count suggested as the `limit` of a retrieval read (#4234).
 *
 * The stand-in has to name a concrete number: told only to "read in ranges",
 * a model reads the whole file, which reloads the payload spilling just removed.
 * 500 lines is a few KiB of typical log output — enough to make progress in one
 * read, small enough that a handful of reads still costs less than the payload.
 */
const RETRIEVAL_CHUNK_LINES = 500;

/**
 * Read the spill threshold from the environment, falling back to
 * DEFAULT_SPILL_THRESHOLD_BYTES.
 *
 * Setting `AGENT_SPILL_THRESHOLD_BYTES` impossibly high is the documented
 * escape hatch: it disables spilling without a rebuild or a revert.
 * Unparseable and non-positive values fall back to the default rather than
 * silently disabling the feature.
 */
export function getSpillThresholdBytes(
  env: Record<string, string | undefined> = process.env,
): number {
  const raw = env.AGENT_SPILL_THRESHOLD_BYTES;
  if (raw === undefined || raw.trim() === '') return DEFAULT_SPILL_THRESHOLD_BYTES;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_SPILL_THRESHOLD_BYTES;
  return Math.floor(parsed);
}

/**
 * Render a tool response as the text we measure and (if oversized) spill.
 *
 * `tool_response` is `unknown` in the SDK types. Strings pass through as-is;
 * everything else is JSON-serialized. Returns null when there is nothing
 * meaningful to spill (null/undefined, or a value JSON cannot represent),
 * which the hook treats as "leave it alone".
 */
export function serializeToolResponse(response: unknown): string | null {
  if (response === null || response === undefined) return null;
  if (typeof response === 'string') return response;
  try {
    const json = JSON.stringify(response);
    // JSON.stringify returns undefined for functions/symbols.
    return json === undefined ? null : json;
  } catch {
    // Circular structures, BigInt, throwing toJSON — not spillable.
    return null;
  }
}

/** U+FFFD REPLACEMENT CHARACTER — what a mid-codepoint UTF-8 cut decodes to. */
const REPLACEMENT_CHAR = '�';
/**
 * A cut inside a UTF-8 sequence can orphan at most 3 continuation bytes, each
 * of which decodes to its own replacement character. Bounding the strip at 3
 * means a payload that legitimately contains U+FFFD keeps it.
 */
const MAX_ORPHAN_BYTES = 3;

/**
 * Slice the first `maxBytes` bytes of `text` without splitting a multi-byte
 * character. Truncating a UTF-8 buffer mid-codepoint produces mojibake in the
 * stand-in — replacement characters where the model expected content.
 */
export function headBytes(text: string, maxBytes: number): string {
  const buf = Buffer.from(text, 'utf8');
  if (buf.length <= maxBytes) return text;
  let decoded = buf.subarray(0, maxBytes).toString('utf8');
  // Drop the trailing replacement char(s) the cut introduced.
  for (let i = 0; i < MAX_ORPHAN_BYTES && decoded.endsWith(REPLACEMENT_CHAR); i++) {
    decoded = decoded.slice(0, -1);
  }
  return decoded;
}

/** Tail counterpart of `headBytes` — drops *leading* partial characters. */
export function tailBytes(text: string, maxBytes: number): string {
  const buf = Buffer.from(text, 'utf8');
  if (buf.length <= maxBytes) return text;
  let decoded = buf.subarray(buf.length - maxBytes).toString('utf8');
  for (let i = 0; i < MAX_ORPHAN_BYTES && decoded.startsWith(REPLACEMENT_CHAR); i++) {
    decoded = decoded.slice(1);
  }
  return decoded;
}

/**
 * Build the compact stand-in the model sees in place of the full payload.
 *
 * Order is deliberate and load-bearing: say what happened, quantify it, show
 * the head, show the tail, then give the locator and the exact instruction for
 * retrieving the rest. The retrieval instruction names `Read`, which is present
 * in both call sites' tool allowlists — the model can always act on it.
 *
 * The instruction is **ranged** (#4234): it names an `offset` that skips the
 * lines already quoted above and a bounded `limit`, because an unranged read
 * pulls the entire payload back into the context that spilling just cleared —
 * the excerpt structure above is only useful if retrieval is chunk-addressable
 * too.
 */
export function formatSpillStandIn(input: {
  toolName: string;
  payload: string;
  locator: string;
  thresholdBytes: number;
  /** Tool the model should use to retrieve the payload. */
  retrievalTool?: string;
}): string {
  const { toolName, payload, locator, thresholdBytes } = input;
  const retrievalTool = input.retrievalTool ?? 'Read';
  const totalBytes = Buffer.byteLength(payload, 'utf8');
  const totalLines = payload.split('\n').length;

  const head = headBytes(payload, HEAD_EXCERPT_BYTES);
  const tail = tailBytes(payload, TAIL_EXCERPT_BYTES);
  const omittedBytes = Math.max(0, totalBytes - Buffer.byteLength(head, 'utf8') - Buffer.byteLength(tail, 'utf8'));

  // Where the un-quoted middle begins. `Read` offsets are 1-indexed lines, and
  // the head excerpt above already covers everything before this point, so this
  // is the first line the model has not already seen.
  const resumeAtLine = head.split('\n').length + 1;

  return [
    `[spilled] Output from \`${toolName}\` was ${totalBytes} bytes (${totalLines} lines), over the ${thresholdBytes}-byte inline limit, so it was written to durable storage instead of being included here in full. Nothing was lost — the complete output is retrievable at the locator below.`,
    '',
    `Full payload: ${totalBytes} bytes, ${totalLines} lines. Shown below: first ${Buffer.byteLength(head, 'utf8')} bytes and last ${Buffer.byteLength(tail, 'utf8')} bytes (${omittedBytes} bytes omitted from the middle).`,
    '',
    '--- HEAD OF OUTPUT ---',
    head,
    `--- ${omittedBytes} BYTES OMITTED ---`,
    '--- TAIL OF OUTPUT ---',
    tail,
    '--- END OF EXCERPT ---',
    '',
    `Locator: ${locator}`,
    `To retrieve more, use the ${retrievalTool} tool on the locator IN RANGES — pass offset and limit, do not read the whole file. The middle section omitted above starts at line ${resumeAtLine}, so start with:`,
    `    ${retrievalTool}(file_path: "${locator}", offset: ${resumeAtLine}, limit: ${RETRIEVAL_CHUNK_LINES})`,
    `Then advance offset by ${RETRIEVAL_CHUNK_LINES} per read (the file has ${totalLines} lines) until you find what you need. Reading it unranged would pull all ${totalBytes} bytes back into this conversation and undo the saving.`,
    `If the excerpt above already answers your question, do not retrieve it at all — that is the point of this summary.`,
  ].join('\n');
}

/**
 * Build a storage key for a spilled payload.
 *
 * `tool_use_id` is unique per call, so the key is collision-free within a run
 * without needing a counter or a clock. Both components are sanitized because
 * the key becomes a path segment.
 */
export function buildSpillKey(toolName: string, toolUseId: string | undefined): string {
  const safe = (s: string) =>
    s
      .replace(/[^A-Za-z0-9._-]/g, '_')
      // Collapse dot runs: `.` has to stay legal (MCP tool names contain it),
      // but `..` must not survive into a path segment. TmpSpillStore rejects
      // escapes as a backstop; this keeps the key from needing that backstop.
      .replace(/\.{2,}/g, '_')
      .slice(0, 80);
  const tool = safe(toolName || 'tool');
  const id = safe(toolUseId || 'no-id');
  return `${tool}-${id}.txt`;
}

/**
 * Does `candidate` live inside a spill directory?
 *
 * Matched per path *segment*, not as a substring: `/w/.adp-spill/x.txt` is a
 * spill file, `/w/.adp-spill-backup/x.txt` and `/w/my.adp-spill.txt` are not.
 * A substring test would exempt unrelated files whose names happen to contain
 * the directory name, which is the over-broad-exemption failure mode in #4234.
 *
 * Both separators are checked so the test does not depend on the host platform.
 */
export function isSpillDirPath(candidate: unknown): boolean {
  if (typeof candidate !== 'string' || candidate === '') return false;
  return candidate.split(/[/\\]/).includes(SPILL_DIR_NAME);
}

/**
 * Is this tool call reading a previously-spilled payload back?
 *
 * This is the fix for #4234. `createSpillHookCallback` decides on payload size
 * alone, so a `Read` of a spilled file — the exact action the stand-in tells the
 * model to take — produced an oversized result and got spilled *again*, handing
 * back a second stand-in instead of the content. The stand-in's promise was
 * therefore unhonourable through the path it advertised.
 *
 * The exemption is deliberately scoped to the **path**, not to the tool. An
 * oversized `Read` of an ordinary file must still spill, otherwise the context
 * bloat this feature exists to prevent walks straight back in through `Read`.
 * (Hence an in-hook guard rather than an SDK `matcher`, which can only match on
 * tool name and so cannot express "Read, but only of these paths".)
 *
 * `tool_input` is `unknown` in the SDK types; `file_path` is what `Read`/`Write`/
 * `Edit` use and `path` is the common alternative among MCP file tools.
 */
export function readsSpilledFile(toolInput: unknown): boolean {
  if (toolInput === null || typeof toolInput !== 'object') return false;
  const input = toolInput as { file_path?: unknown; path?: unknown };
  return isSpillDirPath(input.file_path) || isSpillDirPath(input.path);
}

/**
 * Native SDK tools validate replacements against their own output schemas.
 * A string replacing a Read/Bash object is rejected and the CLI silently keeps
 * the original large output. Keep metadata (especially background/exit state)
 * and replace only known textual fields. Unknown/binary shapes pass through.
 */
function replacementFor(toolName: string, response: unknown, standIn: string): unknown | undefined {
  if (typeof response === 'string') return standIn;
  if (response === null || typeof response !== 'object' || Array.isArray(response)) return undefined;
  const fields = response as Record<string, unknown>;
  if (toolName === 'Bash' && typeof fields.stdout === 'string' && typeof fields.stderr === 'string' && fields.isImage !== true) {
    return { ...fields, stdout: standIn, stderr: '' };
  }
  if (toolName === 'Read' && fields.type === 'text' && fields.file && typeof fields.file === 'object') {
    const file = fields.file as Record<string, unknown>;
    if (typeof file.content === 'string') return { ...fields, file: { ...file, content: standIn, numLines: standIn.split('\n').length } };
  }
  return undefined;
}

export interface SpillHookOptions {
  /** Where oversized payloads are persisted. */
  store: SpillStore;
  /** Structured logger. Spill accounting is emitted here (see below). */
  log?: (msg: string) => void;
  /** Override the threshold; defaults to `getSpillThresholdBytes()`. */
  thresholdBytes?: number;
  /** Tool named in the stand-in's retrieval instruction. Defaults to `Read`. */
  retrievalTool?: string;
}

/**
 * The `PostToolUse` callback itself.
 *
 * Returns `{}` (no rewrite) for anything under the threshold or on any error;
 * returns `hookSpecificOutput.updatedToolOutput` with the stand-in otherwise.
 *
 * Note on logging: the GitHub worker's message loop has no `case 'user'`
 * branch, so it never observes `tool_result` content and cannot count spills
 * itself. Spill accounting therefore has to be emitted from in here — which is
 * also what the smoke test in #4179 greps for.
 */
export function createSpillHookCallback(opts: SpillHookOptions) {
  const log = opts.log ?? ((msg: string) => console.log(msg));

  return async function spillPostToolUse(input: unknown): Promise<Record<string, unknown>> {
    try {
      const hookInput = (input ?? {}) as {
        tool_name?: string;
        tool_input?: unknown;
        tool_response?: unknown;
        tool_use_id?: string;
      };

      // #4234: never spill a read of an already-spilled payload — that turns
      // the stand-in's own retrieval instruction into a loop. Checked before
      // serialization: there is no size at which re-spilling is the right call.
      if (readsSpilledFile(hookInput.tool_input)) return {};

      const payload = serializeToolResponse(hookInput.tool_response);
      if (payload === null) return {};

      const thresholdBytes = opts.thresholdBytes ?? getSpillThresholdBytes();
      const sizeBytes = Buffer.byteLength(payload, 'utf8');
      if (sizeBytes <= thresholdBytes) return {};

      const toolName = hookInput.tool_name ?? 'tool';
      if (replacementFor(toolName, hookInput.tool_response, '') === undefined) {
        log(`[spill] ${toolName} output shape is not supported; keeping original output`);
        return {};
      }
      const key = buildSpillKey(toolName, hookInput.tool_use_id);
      const locator = await opts.store.spill(key, payload);

      const standIn = formatSpillStandIn({
        toolName,
        payload,
        locator,
        thresholdBytes,
        retrievalTool: opts.retrievalTool,
      });

      log(
        `[spill] ${toolName} output ${sizeBytes} bytes > ${thresholdBytes} threshold — ` +
          `spilled to ${locator} (stand-in ${Buffer.byteLength(standIn, 'utf8')} bytes, ` +
          `saved ~${sizeBytes - Buffer.byteLength(standIn, 'utf8')} bytes/turn)`,
      );

      return {
        hookSpecificOutput: {
          hookEventName: 'PostToolUse',
          updatedToolOutput: replacementFor(toolName, hookInput.tool_response, standIn),
        },
      };
    } catch (err) {
      // FAIL OPEN. Returning {} leaves the original output in place, so the
      // worst case is today's behaviour rather than a lost payload or a dead
      // run. See the impact table in #4179.
      log(`[spill] hook failed, passing original output through unmodified: ${(err as Error).message}`);
      return {};
    }
  };
}

/**
 * Build the `hooks` value for the SDK `Options` object.
 *
 * Shaped as `Partial<Record<HookEvent, HookCallbackMatcher[]>>`. No `matcher`
 * is set, so the hook sees every tool — spilling is about payload size, not
 * about which tool produced it. The one exemption (reads of already-spilled
 * files, #4234) is enforced *inside* the callback by `readsSpilledFile`, because
 * a matcher can only select on tool name and the exemption is path-scoped: it
 * must exempt `Read` of a spill file without exempting `Read` in general.
 *
 * Typed loosely (`Record<string, unknown>`) on purpose: both call sites already
 * hand the SDK a loosely-typed options object, and importing the SDK's hook
 * types here would make this module hard to unit-test in isolation.
 */
export function createSpillHooks(opts: SpillHookOptions): Record<string, unknown> {
  return {
    PostToolUse: [{ hooks: [createSpillHookCallback(opts)] }],
  };
}
