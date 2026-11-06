import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { getAccessToken } from '@/services/auth';
import { handleCrewDashboardClick, resolveCrewDashboardDestination } from '@/services/crewDashboard';

vi.mock('@/services/auth', () => ({ getAccessToken: vi.fn(() => 'synthetic-token') }));
vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn(), get: vi.fn() } }));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(getAccessToken).mockReturnValue('synthetic-token');
  vi.mocked(apiClient.post).mockReset();
});
const handoff = 'https://crew.example.test/crew/handoff';
const ticket = 'eyJ0ZW5hbnQiOiJ0ZW5hbnQtYSJ9.c2lnbmF0dXJlLWJ5dGVz';
const destination = `https://crew.example.test/crew/handoff?ticket=${ticket}`;
function browser() {
  const location = { href: '' };
  const alert = vi.fn();
  const fetcher = vi.fn();
  vi.stubGlobal('window', { location, alert });
  vi.stubGlobal('fetch', fetcher);
  return { location, alert, fetcher };
}

describe('Crew dashboard browser handoff', () => {
  it('mints a ticket on the ADP origin and navigates the tab to the Crew host, never fetching it', async () => {
    const { location, alert, fetcher } = browser();
    vi.mocked(apiClient.post).mockResolvedValue({ handoff_url: destination, expires_in: 60 });
    const preventDefault = vi.fn();
    handleCrewDashboardClick({ preventDefault }, handoff);
    await vi.waitFor(() => expect(location.href).toBe(destination));
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/crew/handoff-ticket', {});
    expect(fetcher).not.toHaveBeenCalled();
    expect(alert).not.toHaveBeenCalled();
  });

  it('resolves to the first-party handoff URL for tab-based openers (#7902 CloudFront edge)', async () => {
    const edge = 'https://d111crew.cloudfront.net/crew/handoff';
    const { fetcher } = browser();
    vi.mocked(apiClient.post).mockResolvedValue({ handoff_url: `${edge}?ticket=${ticket}` });
    await expect(resolveCrewDashboardDestination(edge)).resolves.toBe(`${edge}?ticket=${ticket}`);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    'https://attacker.test/crew/handoff?ticket=' + ticket,
    'https://crew.example.test/api/token/local?ticket=' + ticket,
    'https://crew.example.test/?token=single-use-link-12345',
    'https://crew.example.test/crew/handoff?ticket=short',
    `https://crew.example.test/crew/handoff?ticket=${ticket}&ticket=${ticket}`,
    `https://crew.example.test/crew/handoff?ticket=${ticket}&next=/evil`,
    `https://crew.example.test/crew/handoff?ticket=${ticket}#fragment`,
    `https://user:pw@crew.example.test/crew/handoff?ticket=${ticket}`,
    'javascript:alert(1)',
  ])('refuses an untrusted handoff URL %s without navigating', async (handoff_url) => {
    const { location, alert } = browser();
    vi.mocked(apiClient.post).mockResolvedValue({ handoff_url });
    handleCrewDashboardClick({ preventDefault: vi.fn() }, handoff);
    await vi.waitFor(() => expect(alert).toHaveBeenCalledOnce());
    expect(location.href).toBe('');
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });

  it.each([{}, { handoff_url: 42 }, null, 'string'])('refuses a malformed mint response %j', async (body) => {
    const { location, alert } = browser();
    vi.mocked(apiClient.post).mockResolvedValue(body);
    handleCrewDashboardClick({ preventDefault: vi.fn() }, handoff);
    await vi.waitFor(() => expect(alert).toHaveBeenCalledOnce());
    expect(location.href).toBe('');
  });

  it.each([401, 404, 409, 503])('refuses a %d from the mint route without retry or navigation', async (status) => {
    const { location, alert } = browser();
    vi.mocked(apiClient.post).mockRejectedValue({ detail: { error: 'crew_unavailable' }, status });
    handleCrewDashboardClick({ preventDefault: vi.fn() }, handoff);
    await vi.waitFor(() => expect(alert).toHaveBeenCalledOnce());
    expect(apiClient.post).toHaveBeenCalledTimes(1);
    expect(location.href).toBe('');
  });

  it('does not mint a ticket for malformed or non-HTTPS Crew hosts', async () => {
    const { location, alert } = browser();
    for (const bad of ['http://attacker.test/crew/handoff', 'https://crew.example.test/crew/handoff?x=1', 'https://crew.example.test/']) {
      handleCrewDashboardClick({ preventDefault: vi.fn() }, bad);
    }
    await vi.waitFor(() => expect(alert).toHaveBeenCalledTimes(3));
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(location.href).toBe('');
  });

  it('does not attempt handoff without a signed-in ADP identity', () => {
    vi.mocked(getAccessToken).mockReturnValue(null);
    const { location, alert } = browser();
    const preventDefault = vi.fn();
    handleCrewDashboardClick({ preventDefault }, handoff);
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(location.href).toBe('');
    expect(alert).toHaveBeenCalledOnce();
  });
});
