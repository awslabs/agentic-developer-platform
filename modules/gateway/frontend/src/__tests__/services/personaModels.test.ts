import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';

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

describe('personaModelsSelf — self-service request boundaries (design note section 6.1)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('puts no principal identifier anywhere in a self read', async () => {
    const { getPreferences } = await import('@/services/personaModelsSelf');
    vi.mocked(apiClient.get).mockResolvedValue({ entries: [] });
    await getPreferences();
    expect(apiClient.get).toHaveBeenCalledWith('/me/persona-models', undefined);
  });

  it('puts no principal identifier in a self catalogue read', async () => {
    const { getModelCatalogue } = await import('@/services/personaModelsSelf');
    vi.mocked(apiClient.get).mockResolvedValue({ models: [] });
    await getModelCatalogue('malware analysis');
    expect(apiClient.get).toHaveBeenCalledWith(
      '/me/persona-models/catalog?persona_key=malware%20analysis',
      undefined,
    );
  });

  it('omits expected_revision for a genuine create on self path', async () => {
    const { setPreference } = await import('@/services/personaModelsSelf');
    vi.mocked(apiClient.put).mockResolvedValue({});
    await setPreference('developer', 'canonical-model');
    expect(apiClient.put).toHaveBeenCalledWith(
      '/me/persona-models/developer',
      { model: 'canonical-model' },
    );
  });

  it('sends the displayed revision in the self reset body', async () => {
    const { resetPreference } = await import('@/services/personaModelsSelf');
    vi.mocked(apiClient.delete).mockResolvedValue({});
    await resetPreference('developer', 9);
    expect(apiClient.delete).toHaveBeenCalledWith(
      '/me/persona-models/developer',
      { expected_revision: 9 },
    );
  });
});

describe('personaModelsAdmin — administered request boundaries (design note section 6.1)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('uses the opaque server ID only as an encoded administered path segment', async () => {
    const { setPreference } = await import('@/services/personaModelsAdmin');
    vi.mocked(apiClient.put).mockResolvedValue({});
    await setPreference(
      'opaque/id with spaces',
      'architect/reviewer',
      'canonical-model',
      7,
    );
    expect(apiClient.put).toHaveBeenCalledWith(
      '/service-principals/opaque%2Fid%20with%20spaces/persona-models/architect%2Freviewer',
      { model: 'canonical-model', expected_revision: 7 },
    );
  });

  it('uses the target-scoped catalogue for a managed service principal', async () => {
    const { getModelCatalogue } = await import('@/services/personaModelsAdmin');
    vi.mocked(apiClient.get).mockResolvedValue({ models: [] });
    await getModelCatalogue(
      'opaque/id with spaces',
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

  it('sends the displayed revision in the administered reset body', async () => {
    const { resetPreference } = await import('@/services/personaModelsAdmin');
    vi.mocked(apiClient.delete).mockResolvedValue({});
    await resetPreference('service-1', 'developer', 9);
    expect(apiClient.delete).toHaveBeenCalledWith(
      '/service-principals/service-1/persona-models/developer',
      { expected_revision: 9 },
    );
  });
});

describe('self-module boundary — no export accepts a target (design note section 6.1)', () => {
  it('self module exports no function that accepts a scope or principal ID parameter', async () => {
    const selfModule = await import('@/services/personaModelsSelf');
    const exports = Object.entries(selfModule).filter(
      ([, value]) => typeof value === 'function',
    );
    expect(exports.length).toBeGreaterThan(0);
    for (const [, fn] of exports) {
      // Self functions: persona catalogue = 0-1 args (signal), getPreferences = 0-1 args (signal),
      // getModelCatalogue = 1-2 args (personaKey, signal), set/reset = 2-3 args (personaKey, model/revision, ...),
      // getManageableServicePrincipals = 0-1 args (signal).
      // None takes more than 3 positional parameters (the max is setPreference: personaKey, model, expectedRevision).
      // The admin equivalents all add the principalId as a first argument, making them 1 parameter longer.
      const selfArity = (fn as (...args: unknown[]) => unknown).length;
      expect(selfArity).toBeLessThanOrEqual(3);
      // Verify the function's string source does not reference 'scopeBase' (the old shared router)
      const source = (fn as (...args: unknown[]) => unknown).toString();
      expect(source).not.toContain('scopeBase');
    }
    // Every self-module function must route to /me/, never to /service-principals/.
    // getManageableServicePrincipals is excluded: it hits /me/persona-models/manageable-service-principals,
    // which contains the noun "service-principals" in a /me/-rooted self-service URL.
    const dataExports = exports.filter(([name]) => !name.includes('Manageable'));
    for (const [, fn] of dataExports) {
      const source = (fn as (...args: unknown[]) => unknown).toString();
      expect(source).not.toContain('/service-principals/');
    }
  });

  it('self preference read never serialises a principal identifier into the request', async () => {
    const { getPreferences } = await import('@/services/personaModelsSelf');
    vi.mocked(apiClient.get).mockResolvedValue({ entries: [] });
    // Try calling with an extra argument — TypeScript prevents this at compile time,
    // but at runtime we verify the path doesn't carry it.
    await (getPreferences as (...args: unknown[]) => Promise<unknown>)(undefined);
    const [path] = vi.mocked(apiClient.get).mock.calls[0];
    expect(path).toBe('/me/persona-models');
    expect(path).not.toContain('service-principals');
  });
});
