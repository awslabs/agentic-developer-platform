import { invokeCrewHostAwsCredentials } from './crew-host-model-route';
import { startCrewModelAdapter } from './crew-model-adapter';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';

const runId = '11111111-2222-4333-8444-555555555555';
const minted = { Version: 1, AccessKeyId: 'synthetic-access', SecretAccessKey: 'synthetic-secret',
  SessionToken: 'synthetic-session', Expiration: new Date(Date.now() + 900_000).toISOString() };

it('signs the selection with the instance identity and current host token', async () => {
  const send = jest.fn(async () => new Response(JSON.stringify(minted)));
  const result = await invokeCrewHostAwsCredentials({ gatewayUrl: 'https://exampleapi.execute-api.us-east-1.amazonaws.com/dev',
    region: 'us-east-1', modelId: 'example.model', runId, currentRun: () => runId,
    credentials: async () => ({ accessKeyId: 'HOSTACCESS', secretAccessKey: 'synthetic-host-secret', sessionToken: 'synthetic-host-token' }),
    modelToken: async () => 'payload.signature', modelTokenExpiresAt: async () => Math.floor(Date.now() / 1000) + 60,
    send: send as unknown as typeof fetch }, { operation: 'assume', connection_id: 'connection-one' });
  expect(result).toEqual(minted);
  const call = (send.mock.calls as unknown as [string, RequestInit][])[0];
  expect(call[0]).toContain('/agent/crew/aws-credentials');
  expect(JSON.parse(call[1].body as string)).toEqual({ operation: 'assume', connection_id: 'connection-one' });
  expect((call[1].headers as Record<string, string>).authorization).toContain('Credential=HOSTACCESS/');
  expect((call[1].headers as Record<string, string>)['x-adp-crew-model-token']).toBe('payload.signature');
});

it('requires the local capability and reauthorizes each request instead of caching credentials', async () => {
  const requestAwsCredentials = jest.fn().mockResolvedValueOnce(minted).mockRejectedValueOnce(new ChatDataError('denied', 403));
  const adapter = await startCrewModelAdapter({ requestAwsCredentials } as unknown as ChatDataClient, 'example.model');
  try {
    const call = (key = adapter.secret) => fetch(adapter.baseUrl + '/aws/credentials', {
      method: 'POST', headers: { 'x-api-key': key }, body: JSON.stringify({ operation: 'assume' }) });
    expect((await call('wrong')).status).toBe(401);
    expect(await (await call()).json()).toEqual(minted);
    const denied = await call();
    expect(denied.status).toBe(403);
    expect(await denied.text()).not.toContain('synthetic-secret');
    expect(requestAwsCredentials).toHaveBeenCalledTimes(2);
  } finally { await adapter.close(); }
});
