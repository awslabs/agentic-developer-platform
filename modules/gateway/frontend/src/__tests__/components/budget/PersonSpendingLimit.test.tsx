/**
 * Tests for "My spending limit" — Issue #4629 (#4620 · C3), read-only since #4690.
 *
 * The person-facing view of the limit that governs their total agent spend across
 * every workspace. The 2026-09-07 ruling on #4690 made person limits
 * admin-governed only: the self-service write routes were deleted server-side, so
 * this surface renders the applicable limit and its provenance and authors
 * nothing. What is asserted here, and why each one is a gate rather than a
 * coverage line:
 *
 *  - **It renders no figure of its own (#4685).** It is mounted inside the Cloud
 *    spend tile, whose denominator IS this limit. Two renderings of one number on
 *    one page is the ambiguity the #4669 ruling removed.
 *  - **It renders no editor (#4690).** The write routes are GONE, so an input or
 *    a Save/Remove button here is a form whose submit can only 405 — the UI-side
 *    twin of the backend's route-absence pin
 *    (`test_person_cap_routes.py::TestSelfServiceWritesAreGone`).
 *  - **The stopping copy matches what the stored row does.** A `soft` row
 *    (authored under C3) must not claim spend will be stopped, and a `hard` row
 *    (#4630) must not stay silent about the fact that it stops runs. Asserted
 *    against the rendered text, not against a prop.
 *  - **Provenance is the server's sentence, verbatim.** `source_label` is composed
 *    by the ladder resolver — the one place that knows which rung won — and a
 *    client-side recomputation is how the label drifts from the rung actually
 *    enforced (#4511).
 *  - **No limit renders as no limit, never as `$0.00`.** `cap_status` is the
 *    signal; a zero would show a person who may spend nothing.
 *  - **A load failure is not "you have no limit".** An outage is the one moment we
 *    cannot know, so the error copy says so.
 *
 * Fixtures come from `mocks/data/budgetSpend.ts`, transcribed from
 * `src/budget/schemas.py`. Writing them from the frontend type is what let #3675
 * ship a dashboard whose mocks, tests and eval all validated fields the backend
 * never sent.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PersonSpendingLimit } from '@/components/budget/PersonSpendingLimit';
import {
  mockPersonCap,
  mockPersonCapDefaultGoverned,
  mockPersonCapEnforcing,
  mockPersonCapUncapped,
} from '@/mocks/data/budgetSpend';

// Only the read is mocked, because only the read exists: `setMyPersonCap` and
// `deleteMyPersonCap` were deleted with their routes (#4690), so a mock for them
// here would keep a callable alive in tests that the app can no longer reach.
vi.mock('@/services/personCap', () => ({
  getMyPersonCap: vi.fn(),
}));

import { getMyPersonCap } from '@/services/personCap';

const mockGet = getMyPersonCap as ReturnType<typeof vi.fn>;

function createTestQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
}

function renderControl(period: 'daily' | 'weekly' | 'monthly' = 'monthly') {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <PersonSpendingLimit period={period} />
    </QueryClientProvider>,
  );
}

/** Copy that would be false while the limit is informational. */
const ENFORCEMENT_CLAIMS = [/will be stopped/i, /will be blocked/i, /requests are blocked/i, /will be halted/i, /we.ll stop/i];

describe('PersonSpendingLimit — an existing limit', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCap);
  });

  it('renders no money figure of its own (#4685)', async () => {
    // The limit figure is the Cloud spend tile's denominator now. This surface
    // carries the notices and provenance only, so no dollar rendering of the cap
    // may appear here.
    const { container } = renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-current')).toBeInTheDocument());
    expect(container.textContent).not.toMatch(/\$?250(\.00)?/);
  });

  it('a soft row shows the informational notice and makes no enforcement claim', async () => {
    const { container } = renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-informational')).toBeInTheDocument());
    for (const claim of ENFORCEMENT_CLAIMS) {
      expect(container.textContent).not.toMatch(claim);
    }
    expect(screen.queryByTestId('person-cap-enforcing')).not.toBeInTheDocument();
  });

  it('a hard row shows the enforcing notice, not the informational one (#4630)', async () => {
    mockGet.mockResolvedValue(mockPersonCapEnforcing);
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-enforcing')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-enforcing').textContent).toMatch(/stopped/i);
    expect(screen.queryByTestId('person-cap-informational')).not.toBeInTheDocument();
  });

  it("renders the server's provenance sentence verbatim (#4511)", async () => {
    mockGet.mockResolvedValue(mockPersonCapEnforcing);
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-source')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-source').textContent).toContain('a limit set for you by a platform administrator');
  });

  it('a default-governed person sees the default named and the enforcing notice (#4690)', async () => {
    // The pre-#4690 GET reported `uncapped` here — a person a 402 would stop at
    // $100 was told nothing capped them. The response now carries the winning
    // rung, so the screen and the enforcement agree.
    mockGet.mockResolvedValue(mockPersonCapDefaultGoverned);
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-source')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-source').textContent).toContain('org default for org-acme');
    expect(screen.getByTestId('person-cap-enforcing')).toBeInTheDocument();
    expect(screen.queryByTestId('person-cap-uncapped')).not.toBeInTheDocument();
  });

  it('says limits are managed by platform admins', async () => {
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-managed')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-managed').textContent).toMatch(/platform admin/i);
  });
});

describe('PersonSpendingLimit — read-only (#4690 route-absence twin)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCapEnforcing);
  });

  it('renders no editor: no input, no Save/Remove/Change affordance', async () => {
    // The UI-side twin of the backend's `TestSelfServiceWritesAreGone` pin. An
    // editor here posts to routes that no longer exist, so every submit would 405
    // — a form that can only fail is worse than no form.
    const { container } = renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-current')).toBeInTheDocument());
    expect(container.querySelector('input')).toBeNull();
    for (const gone of ['person-cap-edit', 'person-cap-input', 'person-cap-save', 'person-cap-remove', 'person-cap-cancel']) {
      expect(screen.queryByTestId(gone)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: /set a limit|change limit|remove limit|save/i })).not.toBeInTheDocument();
  });

  it('performs the read and nothing else', async () => {
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-current')).toBeInTheDocument());
    expect(mockGet).toHaveBeenCalledWith('monthly');
    // The REAL module (importActual bypasses the mock above) exports no self
    // write now; this pins that a component could not import one back in.
    const services = await vi.importActual<Record<string, unknown>>('@/services/personCap');
    expect('setMyPersonCap' in services).toBe(false);
    expect('deleteMyPersonCap' in services).toBe(false);
  });
});

describe('PersonSpendingLimit — no limit set', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCapUncapped);
  });

  it('states no limit in words, never as $0.00', async () => {
    const { container } = renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-uncapped')).toBeInTheDocument());
    expect(container.textContent).not.toMatch(/\$0(\.00)?/);
    expect(screen.queryByTestId('person-cap-informational')).not.toBeInTheDocument();
    expect(screen.queryByTestId('person-cap-enforcing')).not.toBeInTheDocument();
    expect(screen.queryByTestId('person-cap-source')).not.toBeInTheDocument();
  });

  it('still points at platform admins — "uncapped" is not "unmanageable"', async () => {
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-managed')).toBeInTheDocument());
  });
});

describe('PersonSpendingLimit — failures', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('a load failure is NOT rendered as "no limit"', async () => {
    mockGet.mockRejectedValue(new Error('boom'));
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-error')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-error').textContent).toMatch(/not a statement that you have no limit/i);
    expect(screen.queryByTestId('person-cap-uncapped')).not.toBeInTheDocument();
    expect(screen.queryByTestId('person-cap-managed')).not.toBeInTheDocument();
  });

  it('Retry refetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(mockPersonCapEnforcing);
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-error')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(screen.getByTestId('person-cap-current')).toBeInTheDocument());
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('an account with no cross-workspace identity gets a calm note, not an outage alert', async () => {
    // Since #4690 the GET resolves linked-identity-less accounts through their
    // canonical user id, so this 422 survives only for accounts with no user
    // record at all. For those, a red alert with a Retry that can never succeed
    // reads as a backend failure; the note is a property of the account.
    mockGet.mockRejectedValue({ error: 'unresolvable_person_anchor' });
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-unlinked')).toBeInTheDocument());
    expect(screen.queryByTestId('person-cap-error')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.queryByTestId('person-cap-uncapped')).not.toBeInTheDocument();
  });

  it('detects the anchor code nested in a detail envelope', async () => {
    mockGet.mockRejectedValue({ detail: { error: 'unresolvable_person_anchor', message: 'no linked identity' } });
    renderControl();
    await waitFor(() => expect(screen.getByTestId('person-cap-unlinked')).toBeInTheDocument());
  });
});
