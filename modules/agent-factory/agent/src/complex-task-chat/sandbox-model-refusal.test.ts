import { ChatDataClient, ChatDataError, TURN_ERROR_MESSAGE_MAX_LENGTH } from './gateway/chat-data-client';
import { modelInvokeFailureMessage, modelInvokeRefusal, publishFailedTurn } from './sandbox-model-refusal';

const rejected = (changes: Record<string, string | undefined> = {}) => new ChatDataError('denied', undefined, undefined, undefined, undefined, undefined,
  { status: 'rejected', error_code: 'model_admission_refused', error_class: 'ChatAuthorizationRefusedError', error_detail: 'chat model budget refused', ...changes });

describe('rejected or unknown model-invoke receipts name their reason to the owner (#8048)', () => {
  let client: ChatDataClient;
  let submitTurnResult: jest.SpiedFunction<ChatDataClient['submitTurnResult']>;

  beforeEach(() => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token' });
    submitTurnResult = jest.spyOn(ChatDataClient.prototype, 'submitTurnResult').mockResolvedValue();
  });
  afterEach(() => jest.restoreAllMocks());

  it('builds the refused-before-provider sentence from error_code and the bounded detail', () => {
    expect(modelInvokeFailureMessage(rejected().refusal!)).toBe(
      'The assistant cannot run this turn: the model request was refused before it reached the provider ' +
      '(model_admission_refused: chat model budget refused). Ask the platform operator to check the budget and model readiness for this persona.');
  });

  it('builds the outcome-unknown sentence for a receipt the gateway lost track of after sending', () => {
    expect(modelInvokeFailureMessage({ status: 'unknown', error_code: 'model_outcome_unknown', error_class: 'TimeoutError', error_detail: 'provider timed out' }))
      .toBe('The assistant could not complete this turn: the model request was sent but its outcome is unknown ' +
        '(model_outcome_unknown: provider timed out). Please retry in a moment.');
  });

  it('falls back to the class, then the status, for a gateway that sends less', () => {
    expect(modelInvokeFailureMessage({ status: 'rejected', error_code: 'model_admission_refused', error_class: 'RuntimeError' }))
      .toContain('(model_admission_refused: RuntimeError).');
    expect(modelInvokeFailureMessage({ status: 'rejected' })).toContain('(model_admission_refused).');
    expect(modelInvokeFailureMessage({ status: 'unknown' })).toContain('(model_outcome_unknown).');
  });

  it('keeps the owner text bounded and free of control characters', () => {
    const message = modelInvokeFailureMessage(rejected({ error_detail: `line\x00break\x1f${'x'.repeat(2_000)}` }).refusal!);
    expect(message.length).toBeLessThanOrEqual(TURN_ERROR_MESSAGE_MAX_LENGTH);
    expect(message).toMatch(/^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$/);
    expect(message).toContain('line break');
  });

  it('submits the failed result with the message for a refusal and a plain failed result otherwise', async () => {
    await publishFailedTurn(client, rejected());
    expect(submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed',
      error_message: expect.stringContaining('refused before it reached the provider (model_admission_refused: chat model budget refused)') });
    submitTurnResult.mockClear();
    await publishFailedTurn(client, new ChatDataError('denied', 404));
    await publishFailedTurn(client, new TypeError('fetch failed'));
    expect(submitTurnResult.mock.calls).toEqual([[{ outcome: 'failed' }], [{ outcome: 'failed' }]]);
    expect(modelInvokeRefusal(new TypeError('fetch failed'))).toBeUndefined();
  });

  it('falls back to a plain failed result for a gateway that predates error_message, and surfaces other failures', async () => {
    submitTurnResult.mockRejectedValueOnce(new ChatDataError('invalid_request', 422)).mockResolvedValueOnce();
    await publishFailedTurn(client, rejected());
    expect(submitTurnResult.mock.calls).toEqual([
      [{ outcome: 'failed', error_message: expect.stringContaining('model_admission_refused') }], [{ outcome: 'failed' }],
    ]);
    submitTurnResult.mockReset().mockRejectedValue(new ChatDataError('denied', 404));
    await expect(publishFailedTurn(client, rejected())).rejects.toMatchObject({ code: 'denied' });
    expect(submitTurnResult).toHaveBeenCalledTimes(1);
  });
});
