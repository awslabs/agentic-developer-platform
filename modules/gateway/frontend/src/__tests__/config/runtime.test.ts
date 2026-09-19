import { afterEach, describe, expect, it, vi } from 'vitest';
import { deploymentSetting } from '@/config/runtime';

afterEach(() => {
  delete window.__ADP_CONFIG__;
  vi.unstubAllEnvs();
});

describe('deployment configuration', () => {
  it('uses local Vite settings without a runtime deployment configuration', () => {
    vi.stubEnv('VITE_API_URL', 'http://localhost:8000');
    expect(deploymentSetting('VITE_API_URL')).toBe('http://localhost:8000');
  });

  it('uses target-account settings from the runtime configuration', () => {
    vi.stubEnv('VITE_COGNITO_CLIENT_ID', 'build-account');
    window.__ADP_CONFIG__ = { VITE_COGNITO_CLIENT_ID: 'target-account' };
    expect(deploymentSetting('VITE_COGNITO_CLIENT_ID')).toBe('target-account');
  });

  it('does not fall back to another account for omitted runtime settings', () => {
    vi.stubEnv('VITE_AGENT_WS_URL', 'wss://other-account.example');
    window.__ADP_CONFIG__ = {};
    expect(deploymentSetting('VITE_AGENT_WS_URL')).toBeUndefined();
  });
});
