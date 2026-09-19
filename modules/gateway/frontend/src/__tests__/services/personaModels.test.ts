import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getModelCatalogue,
  getPreferences,
  resetPreference,
  setPreference,
} from '@/services/personaModels';

vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  buildQueryString: (params: Record<string, unknown>) => {
    const value = params.persona_key;
    return value ? `?persona_key=${encodeURIComponent(String(value))}` : '';
  },
}));

describe('personaModels service request boundaries', () => {
  beforeEach(() => vi.clearAllMocks());

  it('puts no principal identifier anywhere in a self read', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ entries: [] });
    await getPreferences({ kind: 'self' });
    expect(apiClient.get).toHaveBeenCalledWith('/me/persona-models', undefined);
  });

  it('uses the opaque server ID only as an encoded administered path segment', async () => {
    vi.mocked(apiClient.put).mockResolvedValue({});
    await setPreference(
      { kind: 'service', canonicalPrincipalId: 'opaque/id with spaces' },
      'architect/reviewer',
      'canonical-model',
      7,
    );
    expect(apiClient.put).toHaveBeenCalledWith(
      '/service-principals/opaque%2Fid%20with%20spaces/persona-models/architect%2Freviewer',
      { model: 'canonical-model', expected_revision: 7 },
    );
  });

  it('omits expected_revision only for a genuine create', async () => {
    vi.mocked(apiClient.put).mockResolvedValue({});
    await setPreference({ kind: 'self' }, 'developer', 'canonical-model');
    expect(apiClient.put).toHaveBeenCalledWith(
      '/me/persona-models/developer',
      { model: 'canonical-model' },
    );
  });

  it('requests only recorded persona-filtered catalogue data', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ models: [] });
    await getModelCatalogue({ kind: 'self' }, 'malware analysis');
    expect(apiClient.get).toHaveBeenCalledWith(
      '/me/persona-models/catalog?persona_key=malware%20analysis',
      undefined,
    );
  });

  it('uses the target-scoped catalogue for a managed service principal', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ models: [] });
    await getModelCatalogue(
      { kind: 'service', canonicalPrincipalId: 'opaque/id with spaces' },
      'architect/reviewer',
    );
    expect(apiClient.get).toHaveBeenCalledWith(
      '/service-principals/opaque%2Fid%20with%20spaces/persona-models/catalog?persona_key=architect%2Freviewer',
      undefined,
    );
    expect(apiClient.get).not.toHaveBeenCalledWith(
      expect.stringMatching(/^\/me\/persona-models\/catalog/),
      expect.anything(),
    );
  });

  it('uses PMM-02’s bodyless reset contract', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue({});
    await resetPreference({ kind: 'self' }, 'developer');
    expect(apiClient.delete).toHaveBeenCalledWith('/me/persona-models/developer');
  });
});
