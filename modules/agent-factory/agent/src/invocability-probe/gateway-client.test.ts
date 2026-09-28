jest.mock('../lib/runIdentity', () => ({
  workerAwsCredentialProvider: jest.fn(async () => async () => ({
    accessKeyId: 'TEST', secretAccessKey: 'TEST', sessionToken: 'TEST',
  })),
  gatewaySigningRegion: jest.fn(() => 'eu-west-2'),
}));

import { SigV4ProbeGateway } from './gateway-client';

describe('SigV4 probe Gateway client', () => {
  afterEach(() => jest.restoreAllMocks());

  it('uses the exact internal claim route with no /agent prefix', async () => {
    const fetchMock = jest.spyOn(global, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      claimed: false,
      reason: 'probing_disabled',
    }), { status: 200, headers: { 'content-type': 'application/json' } }));

    const client = new SigV4ProbeGateway('https://abc.execute-api.eu-west-2.amazonaws.com/dev/');
    await expect(client.claim()).resolves.toEqual({ claimed: false, reason: 'probing_disabled' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://abc.execute-api.eu-west-2.amazonaws.com/dev/internal/v1/persona-model-probes/claim',
    );
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.body).toBe('{"trigger":"scheduled"}');
    expect(init.redirect).toBe('error');
    expect((init.headers as Record<string, string>).authorization).toBeDefined();
  });

  it('sends the fixed start and complete wire contracts', async () => {
    const fetchMock = jest.spyOn(global, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ slot_id: 'slot/1' }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        slot_id: 'slot/1', status: 'completed', evidence_recorded: true,
      }), { status: 200 }));
    const client = new SigV4ProbeGateway('https://gateway.example');
    await client.start('slot/1', 'lease-token-which-is-at-least-32-chars', 'a'.repeat(64));
    await client.complete('slot/1', 'lease-token-which-is-at-least-32-chars', {
      outcome: 'proven',
      request_shape_sha256: 'a'.repeat(64),
      provider_request_id: 'request-1',
      error_code: null,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      'https://gateway.example/internal/v1/persona-model-probes/slot%2F1/start',
    );
    expect((fetchMock.mock.calls[0][1] as RequestInit).body)
      .toBe(`{"lease_token":"lease-token-which-is-at-least-32-chars","request_shape_sha256":"${'a'.repeat(64)}"}`);
    expect(fetchMock.mock.calls[1][0]).toBe(
      'https://gateway.example/internal/v1/persona-model-probes/slot%2F1/complete',
    );
    expect((fetchMock.mock.calls[1][1] as RequestInit).body)
      .toContain('"lease_token":"lease-token-which-is-at-least-32-chars"');
  });
});
