/**
 * Tests for spill-to-storage of oversized tool output (#4179).
 *
 * The test set is organized around the failure modes in the issue's impact
 * table, because those are what actually matter here:
 *   - unreadable locator  → "stand-in is actionable" tests
 *   - threshold too low   → passthrough test
 *   - threshold too high  → threshold-is-asserted test (don't trust the default)
 *   - persist fails       → fail-open tests (the most important in the set)
 *   - mojibake            → multi-byte boundary tests
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  DEFAULT_SPILL_THRESHOLD_BYTES,
  buildSpillKey,
  createSpillHookCallback,
  createSpillHooks,
  formatSpillStandIn,
  getSpillThresholdBytes,
  headBytes,
  serializeToolResponse,
  tailBytes,
  TmpSpillStore,
} from './index';
import { SpillStore } from './store';
import { ArtifactSpillStore } from './artifact-store-adapter';
import { ArtifactStore, ArtifactRef } from '../../complex-task-chat/artifacts/port';

/** Tools allowlisted at both call sites (agent-worker.ts, run-query.ts). */
const SHARED_ALLOWLIST = [
  'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill',
];

/** In-memory SpillStore that records what it was asked to persist. */
class FakeSpillStore implements SpillStore {
  readonly writes: Array<{ key: string; body: string }> = [];
  async spill(key: string, body: string): Promise<string> {
    this.writes.push({ key, body });
    return `/tmp/fake-spill/${key}`;
  }
}

/** SpillStore that always fails — drives the fail-open assertions. */
class ExplodingSpillStore implements SpillStore {
  async spill(): Promise<string> {
    throw new Error('bucket does not exist');
  }
}

function hookInput(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    hook_event_name: 'PostToolUse',
    tool_name: 'Bash',
    tool_input: { command: 'npm test' },
    tool_response: 'ok',
    tool_use_id: 'toolu_abc123',
    ...overrides,
  };
}

/** Payload comfortably over the default threshold. */
function bigPayload(bytes = DEFAULT_SPILL_THRESHOLD_BYTES * 2): string {
  // Distinct head and tail so excerpt assertions can tell them apart.
  const head = 'HEAD_MARKER_LINE\n';
  const tail = '\nTAIL_MARKER_LINE';
  const filler = 'x'.repeat(Math.max(0, bytes - head.length - tail.length));
  return head + filler + tail;
}

let tmpRoot: string;

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'spill-test-'));
  delete process.env.AGENT_SPILL_THRESHOLD_BYTES;
});

afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
  delete process.env.AGENT_SPILL_THRESHOLD_BYTES;
});

// ---------------------------------------------------------------------------
// Threshold configuration
// ---------------------------------------------------------------------------

describe('getSpillThresholdBytes', () => {
  it('defaults to the documented default when the env var is unset', () => {
    // Asserting the constant explicitly rather than trusting it: a silently
    // over-large default means the defect persists with no visible symptom.
    expect(DEFAULT_SPILL_THRESHOLD_BYTES).toBe(20_000);
    expect(getSpillThresholdBytes({})).toBe(DEFAULT_SPILL_THRESHOLD_BYTES);
  });

  it('reads the override from AGENT_SPILL_THRESHOLD_BYTES', () => {
    expect(getSpillThresholdBytes({ AGENT_SPILL_THRESHOLD_BYTES: '512' })).toBe(512);
  });

  it('supports an impossibly-high value as the documented escape hatch', () => {
    const huge = getSpillThresholdBytes({ AGENT_SPILL_THRESHOLD_BYTES: '999999999' });
    expect(huge).toBe(999_999_999);
    expect(Buffer.byteLength(bigPayload(), 'utf8')).toBeLessThan(huge);
  });

  it.each([
    ['not-a-number', 'unparseable'],
    ['0', 'zero'],
    ['-1', 'negative'],
    ['', 'empty'],
  ])('falls back to the default for %s (%s) rather than disabling spilling', (value) => {
    expect(getSpillThresholdBytes({ AGENT_SPILL_THRESHOLD_BYTES: value }))
      .toBe(DEFAULT_SPILL_THRESHOLD_BYTES);
  });
});

// ---------------------------------------------------------------------------
// Serialization
// ---------------------------------------------------------------------------

describe('serializeToolResponse', () => {
  it('passes strings through unchanged', () => {
    expect(serializeToolResponse('hello')).toBe('hello');
  });

  it('JSON-serializes structured responses', () => {
    expect(serializeToolResponse({ stdout: 'a', code: 0 })).toBe('{"stdout":"a","code":0}');
  });

  it.each([[null], [undefined]])(
    'returns null for %s so the hook leaves it alone',
    (value) => {
      expect(serializeToolResponse(value)).toBeNull();
    },
  );

  it('returns null for a circular structure instead of throwing', () => {
    const circular: Record<string, unknown> = { a: 1 };
    circular.self = circular;
    expect(serializeToolResponse(circular)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Multi-byte safety
// ---------------------------------------------------------------------------

describe('multi-byte excerpting', () => {
  // '🙂' is 4 UTF-8 bytes; cutting at 2 bytes lands mid-codepoint.
  const emoji = '🙂'.repeat(20);

  it('headBytes does not emit a replacement char when the cut lands mid-codepoint', () => {
    const head = headBytes(emoji, 10); // 10 is not a multiple of 4
    expect(head).not.toContain('�');
    expect(head).toBe('🙂🙂');
  });

  it('tailBytes does not emit a replacement char when the cut lands mid-codepoint', () => {
    const tail = tailBytes(emoji, 10);
    expect(tail).not.toContain('�');
    expect(tail).toBe('🙂🙂');
  });

  it('returns the input unchanged when it already fits', () => {
    expect(headBytes('short', 100)).toBe('short');
    expect(tailBytes('short', 100)).toBe('short');
  });

  it('produces a mojibake-free stand-in for a multi-byte payload', async () => {
    const payload = '日本語のテスト\n'.repeat(4000); // 3-byte chars, well over threshold
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, log: () => {} });

    const out = await hook(hookInput({ tool_response: payload })) as {
      hookSpecificOutput: { updatedToolOutput: string };
    };

    expect(out.hookSpecificOutput.updatedToolOutput).not.toContain('�');
    // The full payload round-trips byte-exactly regardless of excerpt slicing.
    expect(store.writes[0].body).toBe(payload);
  });
});

// ---------------------------------------------------------------------------
// The stand-in
// ---------------------------------------------------------------------------

describe('formatSpillStandIn', () => {
  const payload = bigPayload();
  const standIn = formatSpillStandIn({
    toolName: 'Bash',
    payload,
    locator: '/tmp/workspace/.adp-spill/Bash-toolu_abc123.txt',
    thresholdBytes: 20_000,
  });

  it('states the byte count and line count of the full payload', () => {
    expect(standIn).toContain(String(Buffer.byteLength(payload, 'utf8')));
    expect(standIn).toContain(String(payload.split('\n').length));
  });

  it('includes a head excerpt', () => {
    expect(standIn).toContain('HEAD_MARKER_LINE');
  });

  it('includes a tail excerpt — errors are usually at the end', () => {
    expect(standIn).toContain('TAIL_MARKER_LINE');
  });

  it('includes the locator', () => {
    expect(standIn).toContain('/tmp/workspace/.adp-spill/Bash-toolu_abc123.txt');
  });

  it('names a retrieval tool present in both call sites\' allowlists', () => {
    // The primary risk in the issue's impact table is a stand-in whose locator
    // the agent cannot act on. Naming a tool it demonstrably has is the guard.
    expect(standIn).toContain('Read');
    expect(SHARED_ALLOWLIST).toContain('Read');
  });

  it('is dramatically smaller than the payload it replaces', () => {
    expect(Buffer.byteLength(standIn, 'utf8')).toBeLessThan(Buffer.byteLength(payload, 'utf8') / 2);
  });

  it('honours a custom retrieval tool', () => {
    const custom = formatSpillStandIn({
      toolName: 'Bash',
      payload,
      locator: 'art_123',
      thresholdBytes: 20_000,
      retrievalTool: 'fetch_artifact',
    });
    expect(custom).toContain('fetch_artifact');
  });
});

describe('buildSpillKey', () => {
  it('combines tool name and tool_use_id', () => {
    expect(buildSpillKey('Bash', 'toolu_abc123')).toBe('Bash-toolu_abc123.txt');
  });

  it('sanitizes path separators so a key cannot traverse directories', () => {
    const key = buildSpillKey('../../etc', 'x/y');
    expect(key).not.toContain('/');
    expect(key).not.toContain('..');
  });

  it('tolerates a missing tool_use_id', () => {
    expect(buildSpillKey('Bash', undefined)).toBe('Bash-no-id.txt');
  });
});

// ---------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------

describe('createSpillHookCallback', () => {
  it('passes below-threshold output through byte-identically (no rewrite)', async () => {
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, log: () => {} });

    const out = await hook(hookInput({ tool_response: 'a short result' }));

    // An empty result means "no change" to the SDK — the original stands.
    expect(out).toEqual({});
    expect(store.writes).toHaveLength(0);
  });

  it('is inert for output exactly at the threshold (boundary is inclusive)', async () => {
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, thresholdBytes: 100, log: () => {} });

    const out = await hook(hookInput({ tool_response: 'x'.repeat(100) }));

    expect(out).toEqual({});
    expect(store.writes).toHaveLength(0);
  });

  it('spills above-threshold output and returns a stand-in with count, head, tail and locator', async () => {
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, log: () => {} });
    const payload = bigPayload();

    const out = await hook(hookInput({ tool_response: payload })) as {
      hookSpecificOutput: { hookEventName: string; updatedToolOutput: string };
    };

    expect(out.hookSpecificOutput.hookEventName).toBe('PostToolUse');
    const standIn = out.hookSpecificOutput.updatedToolOutput;
    expect(standIn).toContain(String(Buffer.byteLength(payload, 'utf8'))); // count
    expect(standIn).toContain('HEAD_MARKER_LINE');                        // head
    expect(standIn).toContain('TAIL_MARKER_LINE');                        // tail
    expect(standIn).toContain('/tmp/fake-spill/Bash-toolu_abc123.txt');   // locator
    expect(standIn).toContain('Read');                                    // retrieval tool
  });

  it('persists the payload verbatim — the feature must not be lossy', async () => {
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, log: () => {} });
    const payload = bigPayload();

    await hook(hookInput({ tool_response: payload }));

    expect(store.writes).toHaveLength(1);
    expect(store.writes[0].body).toBe(payload);
  });

  it('spills oversized structured (non-string) responses too', async () => {
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, log: () => {} });

    const out = await hook(hookInput({ tool_response: { stdout: bigPayload() } }));

    expect(out).not.toEqual({});
    expect(store.writes).toHaveLength(1);
  });

  it('honours an explicit threshold override', async () => {
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, thresholdBytes: 10, log: () => {} });

    const out = await hook(hookInput({ tool_response: 'this is longer than ten bytes' }));

    expect(out).not.toEqual({});
    expect(store.writes).toHaveLength(1);
  });

  it('reads the threshold from the environment when not overridden', async () => {
    process.env.AGENT_SPILL_THRESHOLD_BYTES = '10';
    const store = new FakeSpillStore();
    const hook = createSpillHookCallback({ store, log: () => {} });

    const out = await hook(hookInput({ tool_response: 'this is longer than ten bytes' }));

    expect(out).not.toEqual({});
  });

  it('logs spill accounting (the worker cannot count spills from its message loop)', async () => {
    const logs: string[] = [];
    const hook = createSpillHookCallback({ store: new FakeSpillStore(), log: m => logs.push(m) });

    await hook(hookInput({ tool_response: bigPayload() }));

    // The smoke test in #4179 greps pod logs for this marker.
    expect(logs.join('\n')).toMatch(/\[spill\]/);
  });

  it('emits no spill log line below the threshold', async () => {
    const logs: string[] = [];
    const hook = createSpillHookCallback({ store: new FakeSpillStore(), log: m => logs.push(m) });

    await hook(hookInput({ tool_response: 'tiny' }));

    expect(logs).toHaveLength(0);
  });

  describe('fail-open behaviour', () => {
    // The single most important property: an optimization must never be able to
    // turn a working run into a failed one.
    it('returns the original output unmodified and logs when the store throws', async () => {
      const logs: string[] = [];
      const hook = createSpillHookCallback({
        store: new ExplodingSpillStore(),
        log: m => logs.push(m),
      });

      const out = await hook(hookInput({ tool_response: bigPayload() }));

      expect(out).toEqual({});
      expect(logs.join('\n')).toMatch(/bucket does not exist/);
      expect(logs.join('\n')).toMatch(/passing original output through/);
    });

    it('never rejects, so a store error cannot abort the agent run', async () => {
      const hook = createSpillHookCallback({ store: new ExplodingSpillStore(), log: () => {} });
      await expect(hook(hookInput({ tool_response: bigPayload() }))).resolves.toEqual({});
    });

    it('tolerates a malformed hook input', async () => {
      const hook = createSpillHookCallback({ store: new FakeSpillStore(), log: () => {} });
      await expect(hook(undefined)).resolves.toEqual({});
      await expect(hook({})).resolves.toEqual({});
    });
  });
});

describe('createSpillHooks', () => {
  it('registers exactly one un-matchered PostToolUse hook', () => {
    const hooks = createSpillHooks({ store: new FakeSpillStore() }) as {
      PostToolUse: Array<{ matcher?: string; hooks: unknown[] }>;
    };

    expect(Object.keys(hooks)).toEqual(['PostToolUse']);
    expect(hooks.PostToolUse).toHaveLength(1);
    expect(hooks.PostToolUse[0].hooks).toHaveLength(1);
    // No matcher: spilling is about payload size, not which tool produced it.
    expect(hooks.PostToolUse[0].matcher).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// TmpSpillStore (GitHub worker path)
// ---------------------------------------------------------------------------

describe('TmpSpillStore', () => {
  it('writes the payload and returns a readable absolute path', async () => {
    const store = new TmpSpillStore(tmpRoot);

    const locator = await store.spill('Bash-toolu_1.txt', 'full output here');

    expect(path.isAbsolute(locator)).toBe(true);
    expect(fs.readFileSync(locator, 'utf8')).toBe('full output here');
  });

  it('round-trips a large payload exactly through the locator', async () => {
    const store = new TmpSpillStore(tmpRoot);
    const payload = bigPayload();

    const locator = await store.spill('Bash-toolu_2.txt', payload);

    // This is the property that proves the feature is not lossy: what the
    // agent reads back at the locator is what the tool actually produced.
    expect(fs.readFileSync(locator, 'utf8')).toBe(payload);
  });

  it('places spills under the spill directory inside the run workspace', async () => {
    const store = new TmpSpillStore(tmpRoot);
    const locator = await store.spill('Bash-toolu_3.txt', 'x');
    expect(locator.startsWith(path.resolve(tmpRoot))).toBe(true);
    expect(locator).toContain('.adp-spill');
  });

  it('rejects a key that would escape the spill directory', async () => {
    const store = new TmpSpillStore(tmpRoot);
    await expect(store.spill('../../escaped.txt', 'x')).rejects.toThrow(/escapes/);
  });

  it('still returns a working locator when the best-effort S3 leg fails', async () => {
    // Mirrors the real deployment state: per #4184 the worker's S3 config is
    // unreliable, so the /tmp + Read locator must stand entirely on its own.
    const logs: string[] = [];
    const store = new TmpSpillStore(tmpRoot, {
      uploadToS3: async () => { throw new Error('AccessDenied'); },
      log: m => logs.push(m),
    });

    const locator = await store.spill('Bash-toolu_4.txt', 'payload');

    expect(fs.readFileSync(locator, 'utf8')).toBe('payload');
    expect(logs.join('\n')).toMatch(/S3 upload failed/);
  });

  describe('git exclude registration', () => {
    // The worker's spill root IS the cloned repo and the worker runs
    // `git add -A`, so an unexcluded spill would be committed into the
    // agent's own PR.
    it('excludes the spill dir via .git/info/exclude when the root is a repo', async () => {
      fs.mkdirSync(path.join(tmpRoot, '.git', 'info'), { recursive: true });
      const store = new TmpSpillStore(tmpRoot);

      await store.spill('Bash-toolu_6.txt', 'x');

      const exclude = fs.readFileSync(path.join(tmpRoot, '.git', 'info', 'exclude'), 'utf8');
      expect(exclude.split('\n')).toContain('.adp-spill/');
    });

    it('does not duplicate the entry across multiple spills', async () => {
      fs.mkdirSync(path.join(tmpRoot, '.git', 'info'), { recursive: true });
      const store = new TmpSpillStore(tmpRoot);

      await store.spill('a.txt', 'x');
      await store.spill('b.txt', 'x');

      const exclude = fs.readFileSync(path.join(tmpRoot, '.git', 'info', 'exclude'), 'utf8');
      expect(exclude.split('\n').filter(l => l === '.adp-spill/')).toHaveLength(1);
    });

    it('preserves pre-existing exclude entries', async () => {
      const excludeFile = path.join(tmpRoot, '.git', 'info', 'exclude');
      fs.mkdirSync(path.dirname(excludeFile), { recursive: true });
      fs.writeFileSync(excludeFile, '.adp-rules/\n.claude/skills/\n');
      const store = new TmpSpillStore(tmpRoot);

      await store.spill('a.txt', 'x');

      const exclude = fs.readFileSync(excludeFile, 'utf8');
      expect(exclude).toContain('.adp-rules/');
      expect(exclude).toContain('.claude/skills/');
      expect(exclude).toContain('.adp-spill/');
    });

    it('spills normally when the root is not a git repo', async () => {
      const store = new TmpSpillStore(tmpRoot);
      const locator = await store.spill('a.txt', 'payload');
      expect(fs.readFileSync(locator, 'utf8')).toBe('payload');
      expect(fs.existsSync(path.join(tmpRoot, '.git'))).toBe(false);
    });
  });

  it('invokes the S3 leg with the same key when configured', async () => {
    const uploads: string[] = [];
    const store = new TmpSpillStore(tmpRoot, {
      uploadToS3: async (key) => { uploads.push(key); },
    });

    await store.spill('Bash-toolu_5.txt', 'payload');

    expect(uploads).toEqual(['Bash-toolu_5.txt']);
  });
});

// ---------------------------------------------------------------------------
// ArtifactSpillStore (chat path)
// ---------------------------------------------------------------------------

describe('ArtifactSpillStore', () => {
  function fakeArtifactStore(overrides: Partial<ArtifactStore> = {}) {
    const publish = jest.fn(async (input: { localPath: string }): Promise<ArtifactRef> => ({
      id: 'art_123',
      url: 'https://example/presigned',
      urlExpiresAt: new Date(0).toISOString(),
      filename: path.basename(input.localPath),
      contentType: 'text/plain',
      sizeBytes: fs.statSync(input.localPath).size,
      checksum: 'deadbeef',
      createdAt: new Date(0).toISOString(),
      source: 'agent',
    }));
    return {
      store: {
        publish,
        fetch: jest.fn(),
        listBySession: jest.fn(),
        toolsForTurn: jest.fn(() => []),
        ...overrides,
      } as unknown as ArtifactStore,
      publish,
    };
  }

  it('publishes through the existing artifact store, not a second S3 client', async () => {
    const { store, publish } = fakeArtifactStore();
    const spillStore = new ArtifactSpillStore(
      store,
      { sessionId: 's1', taskId: 't1', identity: { orgId: 'o1', teamId: 'tm1', userId: 'u1' } },
      { stagingDir: tmpRoot },
    );

    await spillStore.spill('Bash-toolu_1.txt', 'big output');

    expect(publish).toHaveBeenCalledTimes(1);
    const arg = publish.mock.calls[0][0] as Record<string, unknown>;
    expect(arg.sessionId).toBe('s1');
    expect(arg.taskId).toBe('t1');
    // Tenant scoping is inherited from the artifact store, per the issue's
    // isolation requirement — no new shared location.
    expect(arg.identity).toEqual({ orgId: 'o1', teamId: 'tm1', userId: 'u1' });
  });

  it('returns a locator the agent can Read, containing the payload verbatim', async () => {
    const { store } = fakeArtifactStore();
    const spillStore = new ArtifactSpillStore(store, { sessionId: 's1' }, { stagingDir: tmpRoot });

    const locator = await spillStore.spill('Bash-toolu_2.txt', 'big output');

    expect(fs.readFileSync(locator, 'utf8')).toBe('big output');
  });

  it('still returns a readable locator when publish fails', async () => {
    const logs: string[] = [];
    const { store } = fakeArtifactStore({
      publish: jest.fn(async () => { throw new Error('DDB throttled'); }) as unknown as ArtifactStore['publish'],
    });
    const spillStore = new ArtifactSpillStore(
      store,
      { sessionId: 's1' },
      { stagingDir: tmpRoot, log: m => logs.push(m) },
    );

    const locator = await spillStore.spill('Bash-toolu_3.txt', 'big output');

    expect(fs.readFileSync(locator, 'utf8')).toBe('big output');
    expect(logs.join('\n')).toMatch(/artifact publish failed/);
  });

  it('end-to-end through the hook: stand-in locator resolves to the full payload', async () => {
    const { store } = fakeArtifactStore();
    const spillStore = new ArtifactSpillStore(store, { sessionId: 's1' }, { stagingDir: tmpRoot });
    const hook = createSpillHookCallback({ store: spillStore, log: () => {} });
    const payload = bigPayload();

    const out = await hook(hookInput({ tool_response: payload })) as {
      hookSpecificOutput: { updatedToolOutput: string };
    };

    // Pull the locator back out of the stand-in the model would see, then read
    // it — exactly what the agent does on the following turn.
    const match = out.hookSpecificOutput.updatedToolOutput.match(/^Locator: (.+)$/m);
    expect(match).not.toBeNull();
    expect(fs.readFileSync(match![1], 'utf8')).toBe(payload);
  });
});
