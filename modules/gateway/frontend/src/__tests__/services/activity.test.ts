/**
 * Issue #4390: the invocation-list filter params must match the backend names.
 *
 * The front end used to send `start_date`/`end_date`/`limit` while the backend
 * reads `since`/`until`/`page_size`. FastAPI drops unknown query params
 * silently, so every request was a clean 200 with an unfiltered default page —
 * the filters looked wired up in the UI and did nothing.
 *
 * All three builders drifted, so all three are asserted here: if only one is
 * covered, another can silently stay broken.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getMyInvocations,
  getMyChains,
  getAllInvocations,
  getMyTranscript,
  getAdminTranscript,
} from '@/services/activity';

vi.mock('@/services/api', async () => {
  const actual = await vi.importActual<typeof import('@/services/api')>('@/services/api');
  return {
    // buildQueryString is the real implementation — these tests also pin its
    // drop-undefined behaviour, which is what keeps absent filters absent.
    buildQueryString: actual.buildQueryString,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
  };
});

/** The URL the service actually requested, as a URLSearchParams. */
function requestedParams(): URLSearchParams {
  const url = vi.mocked(apiClient.get).mock.calls[0][0] as string;
  return new URLSearchParams(url.slice(url.indexOf('?')));
}

const DATE_FILTERS = { since: '2026-06-13', until: '2026-06-13', page_size: 50 };

describe('Activity service — query param names (Issue #4390)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiClient.get).mockResolvedValue({ items: [], chains: [], count: 0, last_key: null });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe.each([
    ['getMyInvocations', getMyInvocations],
    ['getMyChains', getMyChains],
    ['getAllInvocations', getAllInvocations],
  ] as const)('%s', (_name, fetchFn) => {
    it('sends the backend param names', async () => {
      await fetchFn(DATE_FILTERS);
      const params = requestedParams();

      expect(params.get('since')).toBe('2026-06-13');
      expect(params.get('until')).toBe('2026-06-13');
      expect(params.get('page_size')).toBe('50');
    });

    it('never sends the old (silently ignored) names', async () => {
      await fetchFn(DATE_FILTERS);
      const params = requestedParams();

      expect(params.has('start_date')).toBe(false);
      expect(params.has('end_date')).toBe(false);
      expect(params.has('limit')).toBe(false);
    });

    it('omits date filters entirely when not set', async () => {
      await fetchFn({});
      const params = requestedParams();

      expect(params.has('since')).toBe(false);
      expect(params.has('until')).toBe(false);
      // page_size keeps its explicit default
      expect(params.get('page_size')).toBe('20');
    });

    it('still forwards the params that always worked', async () => {
      await fetchFn({
        status: 'complete',
        channel: 'github',
        persona: 'developer',
        last_key: 'cursor-abc',
        include_non_triggering: true,
      });
      const params = requestedParams();

      expect(params.get('status')).toBe('complete');
      expect(params.get('channel')).toBe('github');
      expect(params.get('persona')).toBe('developer');
      expect(params.get('last_key')).toBe('cursor-abc');
      expect(params.get('include_non_triggering')).toBe('true');
    });
  });

  it('getMyChains requests the chain view', async () => {
    await getMyChains(DATE_FILTERS);
    expect(requestedParams().get('view')).toBe('chains');
  });

  it('getAllInvocations targets the admin endpoint', async () => {
    await getAllInvocations(DATE_FILTERS);
    expect(vi.mocked(apiClient.get).mock.calls[0][0]).toContain('/admin/agent-invocations');
  });

  it('getMyInvocations targets the caller-scoped endpoint', async () => {
    await getMyInvocations(DATE_FILTERS);
    expect(vi.mocked(apiClient.get).mock.calls[0][0]).toContain('/me/agent-invocations');
  });
});

describe('transcript redirect boundary', () => {
  it.each(['member', 'admin'])('keeps %s transcript authorization on its first request', async access => {
    const fetcher = vi.fn().mockResolvedValue(new Response('transcript'));
    vi.stubGlobal('fetch', fetcher);
    sessionStorage.setItem('cognito_access_token', 'transcript-token');
    try {
      if (access === 'member') await getMyTranscript('run/one');
      else await getAdminTranscript('run/one', 'tenant');
      expect(fetcher.mock.calls[0][0]).toContain('run%2Fone');
      expect(fetcher.mock.calls[0][1]).toMatchObject({
        redirect: 'error', headers: { Authorization: 'Bearer transcript-token' },
      });
    } finally {
      sessionStorage.removeItem('cognito_access_token');
      vi.unstubAllGlobals();
    }
  });
});
