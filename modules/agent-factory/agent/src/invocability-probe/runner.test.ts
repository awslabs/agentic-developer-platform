jest.mock('../utils/resilientQuery', () => ({ resilientQuery: jest.fn() }));
jest.mock('../harnesses/claude-control', () => ({
  CLAUDE_ADAPTER_ID: 'claude',
  CLAUDE_SDK_VERSION: '0.3.283',
}));

import manifest from './request-shape-manifest.json';
import { assertLocalProbeEnabled } from './index';
import { classifyObservation, expectedRequestShape, runProbeCycle } from './runner';
import {
  PROBE_PROMPT_SHA256,
  PROBE_SDK_DATE,
  denyProbeToolUse,
  probeSdkEnvironment,
  probeSdkOptions,
} from './request-shape';

const capture = {
  path: '/model/model/invoke-with-response-stream',
  requestShapeSha256: 'a'.repeat(64),
  providerRequestId: 'provider-1',
  providerStatus: 200,
  providerErrorCode: null,
  forwarded: true,
};

describe('Claude invocability probe', () => {
  it('fails closed unless the local deployment gate is explicitly enabled', () => {
    expect(() => assertLocalProbeEnabled({})).toThrow('locally disabled');
    expect(() => assertLocalProbeEnabled({ ADP_PERSONA_MODEL_PROBE_ENABLED: 'false' })).toThrow('locally disabled');
    expect(() => assertLocalProbeEnabled({ ADP_PERSONA_MODEL_PROBE_ENABLED: 'true' })).not.toThrow();
  });

  it('pins a generated digest for every Claude catalogue model', () => {
    expect(manifest.probe_prompt_sha256).toBe(PROBE_PROMPT_SHA256);
    expect(manifest.models['global.anthropic.claude-sonnet-5']).toBe(
      '800704b1c354ea9933c789b2fc74c118e4747bdaedd6e22ddd7f90a8a20bd418',
    );
    for (const [model, digest] of Object.entries(manifest.models)) {
      expect(digest).toMatch(/^[0-9a-f]{64}$/);
      expect(expectedRequestShape(model)).toBe(digest);
    }
  });

  it('uses the real bounded SDK options', () => {
    const abortController = new AbortController();
    const options = probeSdkOptions({ modelId: 'model', maxBudgetUsd: 0.01, abortController, env: {} });
    expect(options).toEqual(expect.objectContaining({
      model: 'model',
      maxTurns: 1,
      maxBudgetUsd: 0.01,
      effort: 'low',
      abortController,
      tools: { type: 'preset', preset: 'claude_code' },
      sessionId: '00000000-0000-4000-8000-000000000002',
    }));
  });

  it('pins SDK-injected reminders without normalizing request bytes', () => {
    const env = probeSdkEnvironment({
      baseUrl: 'http://127.0.0.1:1234',
      region: 'us-east-1',
      accessKeyId: 'LOCAL',
      secretAccessKey: 'LOCAL',
      sessionToken: 'LOCAL',
    });
    expect(env.CLAUDE_CODE_OVERRIDE_DATE).toBe(PROBE_SDK_DATE);
    expect(env.CLAUDE_CODE_DISABLE_BUNDLED_SKILLS).toBe('1');
    expect(env.CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS).toBe('1');
    expect(env.CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT).toBe('1');
  });

  it('keeps tool declarations but denies every tool_use before execution', async () => {
    const abortController = new AbortController();
    const options = probeSdkOptions({ modelId: 'model', maxBudgetUsd: 0.01, abortController, env: {} });
    expect(options.tools).toEqual({ type: 'preset', preset: 'claude_code' });
    expect(options.permissionMode).toBe('default');
    await expect(denyProbeToolUse({
      hook_event_name: 'PreToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'touch /tmp/must-not-exist' },
    } as never, 'tool-use-1', { signal: abortController.signal })).resolves.toEqual({
      hookSpecificOutput: expect.objectContaining({
        hookEventName: 'PreToolUse',
        permissionDecision: 'deny',
      }),
    });
    await expect(options.canUseTool?.('Bash', { command: 'touch /tmp/must-not-exist' }, {
      signal: abortController.signal,
      toolUseID: 'tool-use-1',
      requestId: 'request-1',
    })).resolves.toEqual(expect.objectContaining({ behavior: 'deny', toolUseID: 'tool-use-1' }));
  });

  it('rejects the masked zero-cost, one-turn, no-response SDK success', () => {
    expect(classifyObservation({
      assistantResponses: 0, resultSubtype: 'success', numTurns: 1, totalCostUsd: 0,
    }, capture)).toEqual(expect.objectContaining({
      outcome: 'error', error_code: 'masked_zero_cost_no_response',
    }));
  });

  it('requires a provider request ID for proven evidence', () => {
    expect(classifyObservation({
      assistantResponses: 1, resultSubtype: 'success', numTurns: 1, totalCostUsd: 0.001,
    }, { ...capture, providerRequestId: null })).toEqual(expect.objectContaining({
      outcome: 'error', error_code: 'missing_provider_request_id',
    }));
  });

  it('records a real successful harness response as proven', () => {
    expect(classifyObservation({
      assistantResponses: 1, resultSubtype: 'success', numTurns: 1, totalCostUsd: 0.001,
    }, capture)).toEqual({
      outcome: 'proven',
      request_shape_sha256: 'a'.repeat(64),
      provider_request_id: 'provider-1',
      error_code: null,
    });
  });

  it('classifies a provider 4xx as refused with its request ID and code', () => {
    expect(classifyObservation({
      assistantResponses: 0, resultSubtype: null, numTurns: null, totalCostUsd: null,
    }, {
      ...capture, providerStatus: 400, providerErrorCode: 'ValidationException',
    })).toEqual(expect.objectContaining({
      outcome: 'refused',
      provider_request_id: 'provider-1',
      error_code: 'ValidationException',
    }));
  });

  it('does not call a forwarded request with no response a no-request failure', () => {
    expect(classifyObservation({
      assistantResponses: 0, resultSubtype: null, numTurns: null, totalCostUsd: null,
    }, {
      ...capture,
      providerStatus: null,
      providerRequestId: null,
      providerErrorCode: 'provider_response_indeterminate',
    })).toEqual(expect.objectContaining({
      outcome: 'error', error_code: 'provider_response_indeterminate',
    }));
  });

  it('drains admitted slots sequentially until Gateway reports no work', async () => {
    const runOne = jest.fn()
      .mockResolvedValueOnce({ claimed: true, outcome: 'proven', slotId: '1' })
      .mockResolvedValueOnce({ claimed: true, outcome: 'refused', slotId: '2' })
      .mockResolvedValueOnce({ claimed: false, reason: 'cycle_complete' });
    const result = await runProbeCycle({} as never, 100, runOne);
    expect(result).toEqual({
      completed: 2,
      outcomes: { proven: 1, refused: 1, error: 0 },
      stoppedReason: 'cycle_complete',
    });
    expect(runOne).toHaveBeenCalledTimes(3);
  });

  it('stops at the local hard bound without reserving another slot', async () => {
    const runOne = jest.fn().mockResolvedValue({ claimed: true, outcome: 'proven', slotId: '1' });
    await expect(runProbeCycle({} as never, 2, runOne)).rejects.toThrow('local hard bound (2)');
    expect(runOne).toHaveBeenCalledTimes(2);
  });
});
