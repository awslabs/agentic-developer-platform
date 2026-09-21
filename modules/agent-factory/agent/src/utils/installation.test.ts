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

  /**
   * Outbound URL safety (issue #5604, work-package S05).
   *
   * Semgrep rated `fetch` at lines 61/74 as SSRF. The origin is a constant, so
   * no owner value can redirect the request off api.github.com — these tests
   * pin the reachable concern instead: an owner value must not traverse to a
   * *different GitHub endpoint*, and a response must have actually come from
   * api.github.com before its body decides which installation we mint against.
   */
  describe('outbound GitHub URL safety', () => {
    it.each([
      ['path traversal', '../..'],
      ['trailing traversal', 'org-a/..'],
      ['percent-encoded separator', '..%2F..'],
      ['credential-style prefix', '@evil.com'],
      ['host-style suffix', '.evil.com'],
      ['port-style suffix', ':8080'],
      ['embedded slash', 'org-a/installation'],
      ['query injection', 'org-a?x=1'],
      ['empty owner segment', ' '],
    ])('never interpolates a malformed owner (%s) into a request URL', async (_label, owner) => {
      process.env.REPO_OWNER = owner;
      mockFetch.mockImplementation(async (url: string) =>
        /\/app\/installations$/.test(String(url))
          ? jsonResponse(INSTALLATIONS)
          : jsonResponse({ message: 'Not Found' }, false)
      );

      await resolveInstallationId('jwt', { log: jest.fn() });

      // The owner rung is skipped entirely: the only call is the fallback.
      for (const [requested] of mockFetch.mock.calls) {
        expect(String(requested)).toBe('https://api.github.com/app/installations');
      }
    });

    it('warns and falls back rather than failing when the owner is malformed', async () => {
      const log = jest.fn();
      process.env.REPO_OWNER = '../..';
      mockFetch.mockImplementation(async () => jsonResponse(INSTALLATIONS));

      await expect(resolveInstallationId('jwt', { log })).resolves.toBe(String(FOREIGN_ID));
      expect(log).toHaveBeenCalledWith('WARN', expect.stringContaining('malformed'));
    });

    it('still accepts every owner login GitHub can legitimately issue', async () => {
      for (const owner of ['org-a', 'a', 'My-Org-123', 'a'.repeat(39)]) {
        mockFetch.mockReset();
        process.env.REPO_OWNER = owner;
        mockFetch.mockImplementation(async (url: string) =>
          String(url) === `https://api.github.com/orgs/${owner}/installation`
            ? jsonResponse({ id: OWN_ID })
            : jsonResponse({ message: 'Not Found' }, false)
        );

        await expect(resolveInstallationId('jwt')).resolves.toBe(String(OWN_ID));
      }
    });

    it('keeps the api.github.com origin on both request rungs', async () => {
      process.env.REPO_OWNER = 'org-a';
      mockFetch.mockImplementation(async () => jsonResponse({ message: 'Not Found' }, false));

      await resolveInstallationId('jwt', { log: jest.fn() }).catch(() => undefined);

      expect(mockFetch).toHaveBeenCalled();
      for (const [requested] of mockFetch.mock.calls) {
        expect(new URL(String(requested)).origin).toBe('https://api.github.com');
      }
    });

    it('rejects an owner-lookup body delivered from another origin by redirect', async () => {
      process.env.REPO_OWNER = 'org-a';
      const FOREIGN_BODY_ID = 999;
      mockFetch.mockImplementation(async (url: string) => {
        if (/\/installation$/.test(String(url))) {
          // Redirected off-origin: the body is attacker-supplied, not GitHub's.
          return { ...jsonResponse({ id: FOREIGN_BODY_ID }), url: 'https://evil.example/installation' };
        }
        return { ...jsonResponse(INSTALLATIONS), url: 'https://api.github.com/app/installations' };
      });

      const resolved = await resolveInstallationId('jwt', { log: jest.fn() });

      expect(resolved).not.toBe(String(FOREIGN_BODY_ID));
      expect(resolved).toBe(String(FOREIGN_ID));
    });

    it('rejects a fallback listing delivered from another origin by redirect', async () => {
      mockFetch.mockImplementation(async () => ({
        ...jsonResponse([{ id: 999 }]),
        url: 'https://evil.example/app/installations',
      }));

      await expect(resolveInstallationId('jwt', { log: jest.fn() })).resolves.toBeNull();
    });

    it('accepts a same-origin redirect, as GitHub issues for renamed owners', async () => {
      process.env.REPO_OWNER = 'org-a';
      mockFetch.mockImplementation(async () => ({
        ...jsonResponse({ id: OWN_ID }),
        // GitHub redirects /orgs/{old-name}/... to the current name.
        url: 'https://api.github.com/orgs/org-a-renamed/installation',
      }));

      await expect(resolveInstallationId('jwt')).resolves.toBe(String(OWN_ID));
    });
  });
});
