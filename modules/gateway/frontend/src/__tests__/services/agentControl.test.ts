/**
 * Tests for the live run-control API client — Issue #3966.
 *
 * Two things are worth testing here and they are both about honesty rather than
 * plumbing: that a malformed or hostile payload normalises to the *safe* answer
 * (unavailable, no capabilities, unknown command status) rather than to
 * `undefined` leaking into a boolean test, and that every HTTP status the
 * backend can return maps to its own distinct sentence.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@/services/api', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));

import { apiClient } from '@/services/api';
import {
  MAX_INSTRUCTION_CHARS,
  NO_CAPABILITIES,
  describeControlError,
  getControlState,
  newCommandId,
  normalizeControlState,
  sendControlCommand,
  sendSteerCommand,
} from '@/services/agentControl';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;
const mockPost = apiClient.post as ReturnType<typeof vi.fn>;

/** A complete, valid state payload — the shape control_schemas.py serves. */
function validState(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'run-1',
    generation: 3,
    available: true,
    reason: null,
    capabilities: { pause: true, resume: false, steer: false, abort: true },
    state: 'running',
    active_tool_count: 2,
    updated_at: '2026-09-24T10:00:00Z',
    commands: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// normalizeControlState — fail-closed projection
// ---------------------------------------------------------------------------

describe('normalizeControlState', () => {
  it('passes a well-formed payload through field for field', () => {
    const result = normalizeControlState(validState());
    expect(result.run_id).toBe('run-1');
    expect(result.generation).toBe(3);
    expect(result.available).toBe(true);
    expect(result.state).toBe('running');
    expect(result.active_tool_count).toBe(2);
    expect(result.capabilities).toEqual({
      pause: true,
      resume: false,
      steer: false,
      abort: true,
    });
  });

  it.each([null, undefined, 'a string', 42, []])(
    'falls back to unavailable for a non-object payload (%s)',
    (payload) => {
      const result = normalizeControlState(payload, 'run-9');
      expect(result.run_id).toBe('run-9');
      expect(result.available).toBe(false);
      expect(result.state).toBe('unavailable');
      expect(result.capabilities).toEqual(NO_CAPABILITIES);
      expect(result.commands).toEqual([]);
    },
  );

  it('treats a missing capabilities object as no capabilities', () => {
    const result = normalizeControlState(validState({ capabilities: undefined }));
    expect(result.capabilities).toEqual(NO_CAPABILITIES);
  });

  it('does not let a truthy non-boolean enable a capability or availability', () => {
    // The specific fail-open this guards: `if (caps.pause)` would be true for
    // the string "false", offering a button the gateway answers with 501.
    const result = normalizeControlState(
      validState({
        available: 'true',
        capabilities: { pause: 'false', resume: 1, steer: {}, abort: 'yes' },
      }),
    );
    expect(result.available).toBe(false);
    expect(result.capabilities).toEqual(NO_CAPABILITIES);
  });

  it('maps an unrecognised control state to unavailable', () => {
    const result = normalizeControlState(validState({ state: 'definitely_not_a_state' }));
    expect(result.state).toBe('unavailable');
  });

  it('reports an unrecognised command status as unknown rather than dropping it', () => {
    // Dropping the row would make a command the operator submitted vanish;
    // calling it `delivered` would be a lie. `unknown` is the honest answer.
    const result = normalizeControlState(
      validState({
        commands: [{ command_id: 'c1', action: 'pause', status: 'teleported' }],
      }),
    );
    expect(result.commands).toHaveLength(1);
    expect(result.commands[0].status).toBe('unknown');
  });

  it('discards journal entries with no usable command id or action', () => {
    const result = normalizeControlState(
      validState({
        commands: [
          { command_id: '', action: 'pause', status: 'pending' },
          { command_id: 'c2', action: 'explode', status: 'pending' },
          { action: 'pause', status: 'pending' },
          'not an object',
          null,
          { command_id: 'c5', action: 'abort', status: 'applied' },
        ],
      }),
    );
    expect(result.commands.map((entry) => entry.command_id)).toEqual(['c5']);
  });

  it('keeps an unobserved tool count as null, never zero', () => {
    // The distinction the whole panel rests on: "no tools running" is a claim,
    // "we cannot see" is the truth when the worker did not report.
    expect(normalizeControlState(validState({ active_tool_count: null })).active_tool_count).toBeNull();
    expect(
      normalizeControlState(validState({ active_tool_count: 'two' })).active_tool_count,
    ).toBeNull();
    expect(normalizeControlState(validState({ active_tool_count: 0 })).active_tool_count).toBe(0);
  });

  it('coerces non-string metadata fields to null', () => {
    const result = normalizeControlState(
      validState({ reason: 12, updated_at: {}, generation: 'three' }),
    );
    expect(result.reason).toBeNull();
    expect(result.updated_at).toBeNull();
    expect(result.generation).toBeNull();
  });

  it('normalises per-command timestamps and reason', () => {
    const result = normalizeControlState(
      validState({
        commands: [
          {
            command_id: 'c1',
            action: 'steer',
            status: 'delivered',
            accepted_at: '2026-09-24T10:00:00Z',
            delivered_at: 5,
            reason: 'queued behind a tool call',
          },
        ],
      }),
    );
    expect(result.commands[0].accepted_at).toBe('2026-09-24T10:00:00Z');
    expect(result.commands[0].delivered_at).toBeNull();
    expect(result.commands[0].reason).toBe('queued behind a tool call');
  });

  it('ignores a non-array commands field', () => {
    expect(normalizeControlState(validState({ commands: 'nope' })).commands).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

describe('getControlState', () => {
  it('requests the run-scoped state path and normalises the response', async () => {
    mockGet.mockResolvedValue(validState());
    const result = await getControlState('run-1');
    expect(mockGet).toHaveBeenCalledWith('/activity/invocations/run-1/agent/state', undefined);
    expect(result.state).toBe('running');
  });

  it('encodes an invocation id that arrived from a URL parameter', async () => {
    mockGet.mockResolvedValue(validState());
    await getControlState('run/../../etc');
    expect(mockGet).toHaveBeenCalledWith(
      '/activity/invocations/run%2F..%2F..%2Fetc/agent/state',
      undefined,
    );
  });

  it('forwards an abort signal so a closed modal cancels its poll', async () => {
    const controller = new AbortController();
    mockGet.mockResolvedValue(validState());
    await getControlState('run-1', controller.signal);
    expect(mockGet).toHaveBeenCalledWith(
      '/activity/invocations/run-1/agent/state',
      controller.signal,
    );
  });

  it('rethrows a gateway failure as a control error carrying its status', async () => {
    mockGet.mockRejectedValue({ status: 503, detail: 'live run control is not enabled' });
    await expect(getControlState('run-1')).rejects.toMatchObject({
      status: 503,
      message: 'Live run controls are switched off in this deployment.',
    });
  });

  it('reports a request that never reached the gateway as status 0', async () => {
    mockGet.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(getControlState('run-1')).rejects.toMatchObject({ status: 0 });
  });
});

describe('sendControlCommand', () => {
  it('posts the command id as the idempotency key', async () => {
    mockPost.mockResolvedValue({ command_id: 'cmd-1', command_status: 'pending' });
    await sendControlCommand('run-1', 'pause', 'cmd-1');
    expect(mockPost).toHaveBeenCalledWith('/activity/invocations/run-1/agent/pause', {
      command_id: 'cmd-1',
    });
  });

  it('includes an abort reason only when one was given', async () => {
    mockPost.mockResolvedValue({});
    await sendControlCommand('run-1', 'abort', 'cmd-2', 'wrong branch');
    expect(mockPost).toHaveBeenCalledWith('/activity/invocations/run-1/agent/abort', {
      command_id: 'cmd-2',
      reason: 'wrong branch',
    });
  });

  it('omits the reason key entirely when absent, because extra fields are forbidden', async () => {
    mockPost.mockResolvedValue({});
    await sendControlCommand('run-1', 'resume', 'cmd-3');
    expect(mockPost.mock.calls[0][1]).not.toHaveProperty('reason');
  });

  it('surfaces a 410 as "not applied" rather than as a failure to retry', async () => {
    mockPost.mockRejectedValue({ status: 410, detail: 'run has already finished' });
    await expect(sendControlCommand('run-1', 'abort', 'cmd-4')).rejects.toMatchObject({
      status: 410,
      message: 'This run has already finished. The command was not applied.',
    });
  });
});

describe('sendSteerCommand', () => {
  it('posts the instruction with its command id', async () => {
    mockPost.mockResolvedValue({ command_id: 'cmd-5', command_status: 'pending' });
    await sendSteerCommand('run-1', 'cmd-5', 'use the other branch');
    expect(mockPost).toHaveBeenCalledWith('/activity/invocations/run-1/agent/steer', {
      command_id: 'cmd-5',
      instruction: 'use the other branch',
    });
  });

  it('maps a 501 to "cannot do that yet", which is the honest answer before S6', async () => {
    mockPost.mockRejectedValue({ status: 501, detail: 'steer is not implemented' });
    await expect(sendSteerCommand('run-1', 'cmd-6', 'go')).rejects.toMatchObject({
      status: 501,
      message: 'This deployment cannot perform that action yet.',
    });
  });
});

// ---------------------------------------------------------------------------
// Error copy
// ---------------------------------------------------------------------------

describe('describeControlError', () => {
  it.each([
    [400, /rejected as invalid/i],
    [401, /session has expired/i],
    [403, /not allowed/i],
    [404, /not found, or it is not yours/i],
    [410, /already finished.*not applied/i],
    [413, /too long/i],
    [429, /too many commands/i],
    [501, /cannot perform that action yet/i],
    [502, /could not be reached/i],
    [503, /switched off/i],
    [0, /outcome is unknown/i],
  ])('gives status %i its own sentence', (status, pattern) => {
    expect(describeControlError(status as number)).toMatch(pattern as RegExp);
  });

  it('produces a distinct message for every status it handles', () => {
    // A generic banner for several statuses would leave the reader unable to
    // tell "sign in again" from "shorten the text" from "it already finished".
    const statuses = [400, 401, 403, 404, 409, 410, 413, 429, 501, 502, 503, 0];
    const messages = statuses.map((status) => describeControlError(status));
    expect(new Set(messages).size).toBe(statuses.length);
  });

  it('prefers the server reason for a 409, which explains the specific conflict', () => {
    expect(describeControlError(409, 'control listener not registered')).toBe(
      'control listener not registered',
    );
  });

  it('falls back to generic 409 copy when the server sent no reason', () => {
    expect(describeControlError(409)).toMatch(/cannot accept that command right now/i);
  });

  it('names the real character limit in the 413 message', () => {
    expect(describeControlError(413)).toContain(String(MAX_INSTRUCTION_CHARS));
  });

  it('never claims an unmapped status was applied', () => {
    expect(describeControlError(418)).toMatch(/could not be confirmed/i);
  });
});

// ---------------------------------------------------------------------------
// Command IDs
// ---------------------------------------------------------------------------

describe('newCommandId', () => {
  it('returns a v4 UUID the backend schema will accept', () => {
    expect(newCommandId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
  });

  it('returns a distinct id per call, so two intents are never one key', () => {
    expect(newCommandId()).not.toBe(newCommandId());
  });

  it('still produces a valid UUID where randomUUID is unavailable', () => {
    // Insecure origins and some test environments lack crypto.randomUUID; a
    // throw here would make every control button fail on those.
    const original = globalThis.crypto.randomUUID;
    // @ts-expect-error — deliberately removing the API under test
    globalThis.crypto.randomUUID = undefined;
    try {
      expect(newCommandId()).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
      );
    } finally {
      globalThis.crypto.randomUUID = original;
    }
  });

  it('falls back to Math.random when no crypto API is present at all', () => {
    const original = globalThis.crypto;
    // @ts-expect-error — simulating an environment with no crypto object
    delete globalThis.crypto;
    try {
      expect(newCommandId()).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
      );
    } finally {
      globalThis.crypto = original;
    }
  });
});
