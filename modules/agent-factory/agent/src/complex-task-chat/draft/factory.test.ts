import { buildDraftStore } from './factory';
import { DynamoDraftStore, NoopDraftStore } from './dynamo-draft-store';
import { GatewayDraftStore } from './gateway-draft-store';
import { ChatDataClient } from '../gateway/chat-data-client';

jest.mock('@aws-sdk/client-dynamodb', () => ({ DynamoDBClient: jest.fn() }));
jest.mock('@aws-sdk/lib-dynamodb', () => ({
  DynamoDBDocumentClient: { from: jest.fn(() => ({ send: jest.fn() })) },
  GetCommand: jest.fn(),
  PutCommand: jest.fn(),
}));

describe('buildDraftStore', () => {
  const gateway = {
    client: new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.workload.token' }),
    sessionId: 'session-a',
  };

  it('returns NoopDraftStore when no table is configured and scoped data is off', () => {
    expect(buildDraftStore({})).toBeInstanceOf(NoopDraftStore);
    expect(buildDraftStore({ ADP_CHAT_DATA_ENABLED: 'false' })).toBeInstanceOf(NoopDraftStore);
  });

  it('returns DynamoDraftStore over CONTEXT_TABLE when scoped data is off, even if a client is offered', () => {
    expect(buildDraftStore({ CONTEXT_TABLE: 'chat-context' })).toBeInstanceOf(DynamoDraftStore);
    expect(buildDraftStore({ CONTEXT_TABLE: 'chat-context' }, gateway)).toBeInstanceOf(DynamoDraftStore);
  });

  it('returns GatewayDraftStore only behind the gate with a workload-bound client and session', () => {
    expect(buildDraftStore({ ADP_CHAT_DATA_ENABLED: 'true' }, gateway)).toBeInstanceOf(GatewayDraftStore);
    expect(() => buildDraftStore({ ADP_CHAT_DATA_ENABLED: 'true' })).toThrow('workload-bound client');
    expect(() => buildDraftStore({ ADP_CHAT_DATA_ENABLED: 'true' }, { ...gateway, sessionId: '' })).toThrow('workload-bound client');
  });

  it('does not fall back to the direct table in scoped mode', () => {
    expect(() => buildDraftStore({ ADP_CHAT_DATA_ENABLED: 'true', CONTEXT_TABLE: 'chat-context' })).toThrow('Gateway drafts require');
  });
});
