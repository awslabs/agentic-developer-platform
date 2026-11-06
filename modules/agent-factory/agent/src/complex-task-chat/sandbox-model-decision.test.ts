import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import {
  MODEL_DECISION_RETRIES, MODEL_DECISION_RETRY_DELAY_MS, SandboxModelDecisionError,
  decideTurnModel, modelDecisionFailureMessage, modelDecisionPostureNote, publishModelDecisionFailure,
} from './sandbox-model-decision';

const model = { modelId: 'approved-model', runId: 'run-a', tenantId: 'tenant-a', userId: 'owner', generation: 1,
  posture: 'enforcing' as const, postureVerified: true };
const refusal = (reason: string, retryable = false) => new ChatDataError('unavailable', 503, undefined, undefined,
  { error: 'chat_model_policy_unavailable', reason, retryable, persona: 'developer', model_id: null });
const accepted = { ref: `user_${'a'.repeat(64)}`, message: { role: 'user', content: 'Review this', ts: '2026-10-04T12:00:00Z', tokens: 3, parts: [] } };

describe('refused or unavailable model-policy decision (#7968)', () => {
  let client: ChatDataClient;
  let modelDecision: jest.SpiedFunction<ChatDataClient['modelDecision']>;
  let nextTurn: jest.SpiedFunction<ChatDataClient['nextTurn']>;
  let submitTurnResult: jest.SpiedFunction<ChatDataClient['submitTurnResult']>;
  const sleep = jest.fn(async (_ms: number) => undefined);

  beforeEach(() => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token' });
    modelDecision = jest.spyOn(ChatDataClient.prototype, 'modelDecision');
    nextTurn = jest.spyOn(ChatDataClient.prototype, 'nextTurn').mockResolvedValue(accepted as Awaited<ReturnType<ChatDataClient['nextTurn']>>);
    submitTurnResult = jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockResolvedValue();
    sleep.mockClear();
  });
  afterEach(() => jest.restoreAllMocks());

  it.each([
    ['probing_disabled', false, 'Ask the platform operator to enable model invocability probing.'],
    ['evidence_stale', false, 'Ask the platform operator to enable model invocability probing.'],
    ['not_permitted', false, 'The selected model is not permitted for this persona.'],
    ['runtime_posture_unavailable', true, 'Please retry in a moment.'],
    ['snapshot_missing', false, 'Ask the platform operator to check the model policy for this persona.'],
  ])('names the reason %s and gives the owner the matching hint', (reason, retryable, hint) => {
    expect(modelDecisionFailureMessage({ reason, retryable })).toBe(
      `The assistant cannot run this turn: the model policy decision was unavailable (${reason}). ${hint}`);
  });

  it('ends an accepted turn with the reasoned failure through the result channel and exits the decision', async () => {
    modelDecision.mockRejectedValue(refusal('probing_disabled'));
    const outcome = decideTurnModel(client, { turnAccepted: true, sleep });
    await expect(outcome).rejects.toBeInstanceOf(SandboxModelDecisionError);
    await expect(outcome).rejects.toMatchObject({ failure: { reason: 'probing_disabled', retryable: false },
      message: 'The assistant cannot run this turn: the model policy decision was unavailable (probing_disabled). ' +
        'Ask the platform operator to enable model invocability probing.' });
    expect(modelDecision).toHaveBeenCalledTimes(1);
    expect(nextTurn).not.toHaveBeenCalled();
    expect(sleep).not.toHaveBeenCalled();
    expect(submitTurnResult).toHaveBeenCalledTimes(1);
    expect(submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
      error_message: expect.stringContaining('model policy decision was unavailable (probing_disabled)') });
  });

  it('accepts the owner turn first when the decision is refused before the turn was read', async () => {
    modelDecision.mockRejectedValue(refusal('not_permitted'));
    await expect(decideTurnModel(client, { turnAccepted: false, sleep })).rejects.toBeInstanceOf(SandboxModelDecisionError);
    expect(nextTurn).toHaveBeenCalledTimes(1);
    expect(nextTurn.mock.invocationCallOrder[0]).toBeLessThan(submitTurnResult.mock.invocationCallOrder[0]);
    expect(submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
      error_message: expect.stringContaining('(not_permitted). The selected model is not permitted for this persona.') });
  });

  it('retries a transient refusal at most twice, two seconds apart, before ending the turn', async () => {
    modelDecision.mockRejectedValue(refusal('runtime_posture_unavailable', true));
    await expect(decideTurnModel(client, { turnAccepted: true, sleep })).rejects.toMatchObject({
      failure: { reason: 'runtime_posture_unavailable', retryable: true } });
    expect(MODEL_DECISION_RETRIES).toBe(2);
    expect(MODEL_DECISION_RETRY_DELAY_MS).toBe(2_000);
    expect(modelDecision).toHaveBeenCalledTimes(1 + MODEL_DECISION_RETRIES);
    expect(sleep.mock.calls).toEqual([[2_000], [2_000]]);
    expect(submitTurnResult).toHaveBeenCalledTimes(1);
    expect(submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
      error_message: expect.stringContaining('(runtime_posture_unavailable). Please retry in a moment.') });
  });

  it('uses the decision once a transient refusal clears', async () => {
    modelDecision.mockRejectedValueOnce(refusal('decision_unavailable', true)).mockResolvedValueOnce(model);
    await expect(decideTurnModel(client, { turnAccepted: true, sleep })).resolves.toEqual(model);
    expect(sleep.mock.calls).toEqual([[2_000]]);
    expect(submitTurnResult).not.toHaveBeenCalled();
  });

  it('never retries a policy refusal, even when a transient handler would', async () => {
    modelDecision.mockRejectedValue(refusal('evidence_stale'));
    const retryTransient = jest.fn(async () => true);
    await expect(decideTurnModel(client, { turnAccepted: true, sleep, retryTransient })).rejects.toBeInstanceOf(SandboxModelDecisionError);
    expect(retryTransient).not.toHaveBeenCalled();
    expect(modelDecision).toHaveBeenCalledTimes(1);
  });

  it('leaves an unstructured outage to the existing transient handling and publishes nothing', async () => {
    const outage = new ChatDataError('unavailable', 503);
    modelDecision.mockRejectedValueOnce(outage).mockRejectedValueOnce(outage).mockResolvedValueOnce(model);
    const retryTransient = jest.fn(async () => true);
    await expect(decideTurnModel(client, { turnAccepted: true, sleep, retryTransient })).resolves.toEqual(model);
    expect(retryTransient).toHaveBeenCalledTimes(2);
    retryTransient.mockResolvedValue(false);
    modelDecision.mockRejectedValue(outage);
    await expect(decideTurnModel(client, { turnAccepted: true, sleep, retryTransient })).rejects.toBe(outage);
    expect(submitTurnResult).not.toHaveBeenCalled();
    expect(nextTurn).not.toHaveBeenCalled();
  });

  it.each([new ChatDataError('denied', 404), new ChatDataError('invalid_response'), new TypeError('fetch failed')])(
    'rethrows %s unchanged without publishing a result', async failure => {
      modelDecision.mockRejectedValue(failure);
      await expect(decideTurnModel(client, { turnAccepted: true, sleep })).rejects.toBe(failure);
      expect(submitTurnResult).not.toHaveBeenCalled();
    },
  );

  it('still ends the turn when the result cannot be recorded, and stops when the sandbox is aborted', async () => {
    modelDecision.mockRejectedValue(refusal('probing_disabled'));
    submitTurnResult.mockRejectedValue(new ChatDataError('denied', 404));
    await expect(decideTurnModel(client, { turnAccepted: true, sleep })).rejects.toBeInstanceOf(SandboxModelDecisionError);
    const aborted = AbortSignal.abort(new Error('stopped'));
    await expect(decideTurnModel(client, { turnAccepted: true, sleep, abort: aborted })).rejects.toThrow('stopped');
    expect(modelDecision).toHaveBeenCalledTimes(1);
  });

  it('falls back to a plain failed result for a gateway that predates error_message', async () => {
    submitTurnResult.mockRejectedValueOnce(new ChatDataError('invalid_request', 422)).mockResolvedValueOnce();
    await publishModelDecisionFailure(client, { reason: 'probing_disabled', retryable: false }, { turnAccepted: true });
    expect(submitTurnResult.mock.calls).toEqual([
      [{ outcome: 'failed', error_message: expect.stringContaining('(probing_disabled)') }],
      [{ outcome: 'failed' }],
    ]);
  });
});

describe('advisory and off chat model-policy postures (#7997)', () => {
  let client: ChatDataClient;
  let modelDecision: jest.SpiedFunction<ChatDataClient['modelDecision']>;
  let submitTurnResult: jest.SpiedFunction<ChatDataClient['submitTurnResult']>;
  const warn = jest.fn((_message: string) => undefined);

  beforeEach(() => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token' });
    modelDecision = jest.spyOn(ChatDataClient.prototype, 'modelDecision');
    submitTurnResult = jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockResolvedValue();
    warn.mockClear();
  });
  afterEach(() => jest.restoreAllMocks());

  it('runs the turn on an advisory decision issued despite rejected evidence and notes it in the pod log', async () => {
    const advisory = { ...model, posture: 'advisory' as const, postureVerified: false, rejectionReason: 'probing_disabled' };
    modelDecision.mockResolvedValue(advisory);
    await expect(decideTurnModel(client, { turnAccepted: true, warn })).resolves.toEqual(advisory);
    expect(submitTurnResult).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith('Model policy posture advisory: running approved-model on advice, evidence unverified (probing_disabled)');
  });

  it('runs the turn on a verified advisory or an off decision', async () => {
    const verified = { ...model, posture: 'advisory' as const, postureVerified: true };
    modelDecision.mockResolvedValueOnce(verified);
    await expect(decideTurnModel(client, { turnAccepted: true, warn })).resolves.toEqual(verified);
    expect(warn).toHaveBeenLastCalledWith('Model policy posture advisory: running approved-model on advice, evidence verified');
    const off = { ...model, posture: 'off' as const, postureVerified: true };
    modelDecision.mockResolvedValueOnce(off);
    await expect(decideTurnModel(client, { turnAccepted: true, warn })).resolves.toEqual(off);
    expect(warn).toHaveBeenLastCalledWith('Model policy posture off: running approved-model on advice, evidence verified');
    expect(submitTurnResult).not.toHaveBeenCalled();
  });

  it('says nothing for an enforcing decision', async () => {
    modelDecision.mockResolvedValue(model);
    await expect(decideTurnModel(client, { turnAccepted: true, warn })).resolves.toEqual(model);
    expect(warn).not.toHaveBeenCalled();
    expect(modelDecisionPostureNote(model)).toBeUndefined();
  });

  it('keeps the terminal failure for a genuine refusal or outage under any posture', async () => {
    modelDecision.mockRejectedValue(refusal('not_permitted'));
    await expect(decideTurnModel(client, { turnAccepted: true, warn, sleep: async () => undefined })).rejects.toBeInstanceOf(SandboxModelDecisionError);
    expect(submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
      error_message: expect.stringContaining('(not_permitted). The selected model is not permitted for this persona.') });
    expect(warn).not.toHaveBeenCalled();
  });
});
