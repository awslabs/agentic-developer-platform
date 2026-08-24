/**
 * Behavioral tests for the shared installation-resolution ladder (issue #4071).
 *
 * This ladder previously existed only as an unexported copy inside
 * agent-worker.ts, while utils/ghPost.ts had a separate `installations[0]`
 * implementation. It now lives here so both callers bind to the run's own
 * installation. These tests assert the resolved id, not source text.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { resolveInstallationId } from './installation';

const mockFetch = jest.fn();
global.fetch = mockFetch as any;

function jsonResponse(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 404, json: async () => body };
}

const FOREIGN_ID = 111; // org-b, first in the API listing
const OWN_ID = 222; // org-a, the target

const INSTALLATIONS = [
  { id: FOREIGN_ID, account: { login: 'org-b' } },
  { id: OWN_ID, account: { login: 'org-a' } },
];

describe('resolveInstallationId', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    mockFetch.mockReset();
    delete process.env.GH_APP_INSTALLATION_ID;
    delete process.env.REPO_OWNER;
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('returns GH_APP_INSTALLATION_ID without any network call', async () => {
    process.env.GH_APP_INSTALLATION_ID = String(OWN_ID);
    process.env.REPO_OWNER = 'org-a';

    await expect(resolveInstallationId('jwt')).resolves.toBe(String(OWN_ID));
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('resolves by owner via /orgs/{owner}/installation', async () => {
    process.env.REPO_OWNER = 'org-a';
    mockFetch.mockImplementation(async (url: string) =>
      /\/orgs\/org-a\/installation$/.test(String(url))
        ? jsonResponse({ id: OWN_ID })
        : jsonResponse({ message: 'Not Found' }, false)
    );

    await expect(resolveInstallationId('jwt')).resolves.toBe(String(OWN_ID));
  });

  it('falls through to /users/{owner}/installation when the org lookup 404s', async () => {
    process.env.REPO_OWNER = 'org-a';
    mockFetch.mockImplementation(async (url: string) => {
      const target = String(url);
      if (/\/orgs\/org-a\/installation$/.test(target)) return jsonResponse({ message: 'Not Found' }, false);
      if (/\/users\/org-a\/installation$/.test(target)) return jsonResponse({ id: OWN_ID });
      return jsonResponse({ message: 'Not Found' }, false);
    });

    await expect(resolveInstallationId('jwt')).resolves.toBe(String(OWN_ID));
  });

  it('never returns the foreign installations[0] when the owner IS resolvable', async () => {
    process.env.REPO_OWNER = 'org-a';
    mockFetch.mockImplementation(async (url: string) => {
      const target = String(url);
      if (/\/orgs\/org-a\/installation$/.test(target)) return jsonResponse({ id: OWN_ID });
      if (/\/app\/installations$/.test(target)) return jsonResponse(INSTALLATIONS);
      return jsonResponse({ message: 'Not Found' }, false);
    });

    await expect(resolveInstallationId('jwt')).resolves.not.toBe(String(FOREIGN_ID));
  });

  it('uses installations[0] only as a last resort, and warns', async () => {
    // No owner and no explicit id — the only rung left.
    const log = jest.fn();
    mockFetch.mockImplementation(async (url: string) =>
      /\/app\/installations$/.test(String(url))
        ? jsonResponse(INSTALLATIONS)
        : jsonResponse({ message: 'Not Found' }, false)
    );

    await expect(resolveInstallationId('jwt', { log })).resolves.toBe(String(FOREIGN_ID));
    expect(log).toHaveBeenCalledWith('WARN', expect.stringContaining('last resort'));
  });

  it('warns before falling back when an owner was set but unresolvable', async () => {
    const log = jest.fn();
    process.env.REPO_OWNER = 'org-a';
    mockFetch.mockImplementation(async (url: string) =>
      /\/app\/installations$/.test(String(url))
        ? jsonResponse(INSTALLATIONS)
        : jsonResponse({ message: 'Not Found' }, false)
    );

    await resolveInstallationId('jwt', { log });

    expect(log).toHaveBeenCalledWith(
      'WARN',
      expect.stringContaining('Could not resolve installation for owner org-a')
    );
  });

  it('returns null when the App has no installations at all', async () => {
    mockFetch.mockImplementation(async () => jsonResponse([]));

    await expect(resolveInstallationId('jwt')).resolves.toBeNull();
  });

  it('prefers explicit options over environment variables', async () => {
    process.env.GH_APP_INSTALLATION_ID = '999';

    await expect(
      resolveInstallationId('jwt', { installationId: String(OWN_ID) })
    ).resolves.toBe(String(OWN_ID));
  });

  it('treats a thrown owner lookup as a miss and tries the next rung', async () => {
    process.env.REPO_OWNER = 'org-a';
    mockFetch.mockImplementation(async (url: string) => {
      const target = String(url);
      if (/\/orgs\/org-a\/installation$/.test(target)) throw new Error('ECONNRESET');
      if (/\/users\/org-a\/installation$/.test(target)) return jsonResponse({ id: OWN_ID });
      return jsonResponse({ message: 'Not Found' }, false);
    });

    await expect(resolveInstallationId('jwt')).resolves.toBe(String(OWN_ID));
  });
});
