import { buildArtifactStore } from './factory';
import { NoopArtifactStore } from './noop-artifact-store';
import { GatewayArtifactStore } from './gateway-artifact-store';
import { ChatDataClient } from '../gateway/chat-data-client';

describe('buildArtifactStore', () => {
  it('returns NoopArtifactStore by default', () => {
    const store = buildArtifactStore({});
    expect(store).toBeInstanceOf(NoopArtifactStore);
  });

  it('returns NoopArtifactStore when ARTIFACT_STRATEGY=noop', () => {
    const store = buildArtifactStore({ ARTIFACT_STRATEGY: 'noop' });
    expect(store).toBeInstanceOf(NoopArtifactStore);
  });

  it('throws for unknown strategy', () => {
    expect(() => buildArtifactStore({ ARTIFACT_STRATEGY: 'unknown' })).toThrow(
      'Unknown ARTIFACT_STRATEGY: unknown',
    );
  });

  it('throws when s3 strategy missing ARTIFACTS_BUCKET', () => {
    expect(() => buildArtifactStore({ ARTIFACT_STRATEGY: 's3' })).toThrow(
      'ARTIFACTS_BUCKET env var is required',
    );
  });

  it('constructs gateway artifacts only behind the gate with an injected workload client', () => {
    const gateway = {
      client: new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.workload.token' }),
      sessionId: 'session-a', workspaceRoot: '/workspace',
    };
    expect(buildArtifactStore({ ARTIFACT_STRATEGY: 'gateway', ADP_CHAT_DATA_ENABLED: 'true' }, gateway)).toBeInstanceOf(GatewayArtifactStore);
    expect(() => buildArtifactStore({ ARTIFACT_STRATEGY: 'gateway' }, gateway)).toThrow('require enabled scoped chat data');
    expect(() => buildArtifactStore({ ARTIFACT_STRATEGY: 'gateway', ADP_CHAT_DATA_ENABLED: 'true' })).toThrow('workload-bound client');
  });

  it.each(['s3', 'noop', undefined])('does not fall back to %s in scoped mode', strategy => {
    expect(() => buildArtifactStore({ ADP_CHAT_DATA_ENABLED: 'true', ARTIFACT_STRATEGY: strategy })).toThrow('requires ARTIFACT_STRATEGY=gateway');
  });
});
