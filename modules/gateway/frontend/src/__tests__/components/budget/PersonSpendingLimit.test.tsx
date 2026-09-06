/**
 * Tests for "My spending limit" — Issue #4629 (#4620 · C3).
 *
 * The control a person uses to set a ceiling on their own agent spend across every
 * workspace. What is asserted here, and why each one is a gate rather than a
 * coverage line:
 *
 *  - **It renders no figure of its own (#4685).** It is a control mounted inside the
 *    Cloud spend tile, whose denominator IS this limit. Two renderings of one number
 *    on one page is the ambiguity the #4669 ruling removed.
 *
 *  - **The stopping copy matches what the stored row does.** Both directions of one
 *    rule: a `soft` row (authored under C3) must not claim spend will be stopped,
 *    and a `hard` row (#4630 made the layer enforce) must not stay silent about the
 *    fact that it stops runs. A screen that threatens a consequence it cannot
 *    deliver trains users to disbelieve the screen — the same rule the shadow-mode
 *    banner follows — and a screen that quietly acquires a consequence it never
 *    mentioned is that defect mirrored. Asserted against the rendered text, not
 *    against a prop.
 *  - **No limit renders as no limit, never as `$0.00`.** `cap_status` is the
 *    signal; a zero would show a person who may spend nothing.
 *  - **A load failure is not "you have no limit".** An outage is the one moment we
 *    cannot know, so the error copy says so.
 *  - **The typed amount is what gets sent**, at 2dp, as a string.
 *  - **Removing a limit is a DELETE**, not a `PUT` of `0`.
 *  - **No target is ever sent.** The self surface derives the person from the
 *    token; a component that sent an anchor would be the beginning of the authority
 *    inversion §4.2 forbids.
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
import { PersonSpendingLimit, validateCapAmount } from '@/components/budget/PersonSpendingLimit';
import { mockPersonCap, mockPersonCapEnforcing, mockPersonCapUncapped } from '@/mocks/data/budgetSpend';

vi.mock('@/services/personCap', () => ({
  getMyPersonCap: vi.fn(),
  setMyPersonCap: vi.fn(),
  deleteMyPersonCap: vi.fn(),
}));

import { deleteMyPersonCap, getMyPersonCap, setMyPersonCap } from '@/services/personCap';

const mockGet = getMyPersonCap as ReturnType<typeof vi.fn>;
const mockSet = setMyPersonCap as ReturnType<typeof vi.fn>;
const mockDelete = deleteMyPersonCap as ReturnType<typeof vi.fn>;

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
    mockSet.mockResolvedValue(mockPersonCap);
    mockDelete.mockResolvedValue(undefined);
  });

  it('renders no money figure of its own (#4685)', async () => {
    // The limit figure is the Cloud spend tile's denominator now, and this is a
    // control mounted inside that tile. It used to restate the same number a few
    // lines below the tile's copy of it, which is precisely the "which figure
    // governs me?" ambiguity the #4669 ruling removed — one number, one rendering.
    // Asserted as the absence of ANY dollar amount rather than of `$250.00`
    // specifically, so a future edit cannot reintroduce a figure under a new value.
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-current')).toBeInTheDocument());
    expect(screen.getByTestId('person-spending-limit').textContent ?? '').not.toMatch(/\$\d/);
  });

  it('says the limit is informational and does NOT claim spend will be stopped', async () => {
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-informational')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-informational')).toHaveTextContent(/not blocked/i);

    const rendered = screen.getByTestId('person-spending-limit').textContent ?? '';
    for (const claim of ENFORCEMENT_CLAIMS) {
      expect(rendered).not.toMatch(claim);
    }
  });

  it('tells a soft-row reader they can turn enforcement on by re-saving (#4630)', async () => {
    // A C3-era row keeps its stored `soft` mode, so the informational copy stays
    // true for it — but the mode is no longer permanent, and a person who wants the
    // ceiling to actually bite has no way to discover the one action that does it
    // unless this sentence says so. Nothing else on the screen changes on re-save.
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-informational')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-informational')).toHaveTextContent(/save it again/i);
  });

  it('offers Change limit and Remove limit', async () => {
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-edit')).toHaveTextContent(/change limit/i);
    expect(screen.getByTestId('person-cap-remove')).toBeInTheDocument();
  });
});

describe('PersonSpendingLimit — an enforcing limit (#4630)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCapEnforcing);
    mockSet.mockResolvedValue(mockPersonCapEnforcing);
    mockDelete.mockResolvedValue(undefined);
  });

  it('says the limit stops runs, and does NOT show the informational copy', async () => {
    // C3's rule read one way — never claim spend will be stopped while nothing
    // stops it — and this is the same rule read the other way. A `hard` row halts
    // runs mid-flight; a screen that stayed silent about that would leave the
    // person to discover it from a stopped agent, which is the identical defect
    // (screen and behaviour disagreeing) with the sign flipped.
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-enforcing')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-enforcing')).toHaveTextContent(/stopped/i);
    // The two notices are mutually exclusive: "requests are not blocked" beside
    // "your runs are stopped" is worse than either sentence alone.
    expect(screen.queryByTestId('person-cap-informational')).not.toBeInTheDocument();
  });

  it('states the overshoot bound instead of promising a hard stop', async () => {
    // The denominator is the settled ledger (§5.5 — a person key cannot carry the
    // Redis hash tag the atomic reservation needs), so spend still being metered is
    // invisible to the check and the stop lands slightly over. Promising an exact
    // ceiling here would be the screen over-claiming — the same failure C3's
    // informational copy existed to avoid.
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-enforcing')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-enforcing')).toHaveTextContent(/slightly over/i);
  });

  it('still never sends enforcement_mode when re-saving', async () => {
    // The mode is not client-settable in either direction: authoring the cap IS the
    // opt-in (§5.6), so a component offering a soft/hard toggle would be inventing
    // an authority the server does not accept.
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.clear(screen.getByTestId('person-cap-input'));
    await user.type(screen.getByTestId('person-cap-input'), '300.00');
    await user.click(screen.getByTestId('person-cap-save'));

    await waitFor(() => expect(mockSet).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(mockSet.mock.calls[0])).not.toContain('enforcement_mode');
  });
});

describe('PersonSpendingLimit — no limit set', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCapUncapped);
    mockSet.mockResolvedValue(mockPersonCap);
    mockDelete.mockResolvedValue(undefined);
  });

  it('says no limit is set, and never renders $0.00', async () => {
    // `cap_usd: null` means no row exists. A `$0.00` here would render a person who
    // may spend nothing — the opposite of the truth, and a limit nobody authored.
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-uncapped')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-uncapped')).toHaveTextContent(/have not set a personal limit/i);
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('offers Set a limit and hides Remove limit', async () => {
    // There is nothing to remove, and offering the action would imply there is.
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-edit')).toHaveTextContent(/set a limit/i);
    expect(screen.queryByTestId('person-cap-remove')).not.toBeInTheDocument();
  });
});

describe('PersonSpendingLimit — authoring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCapUncapped);
    mockSet.mockResolvedValue(mockPersonCap);
    mockDelete.mockResolvedValue(undefined);
  });

  it('sends the typed amount as a 2dp string for the selected period', async () => {
    const user = userEvent.setup();
    renderControl('weekly');

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.type(screen.getByTestId('person-cap-input'), '250.00');
    await user.click(screen.getByTestId('person-cap-save'));

    await waitFor(() => expect(mockSet).toHaveBeenCalledTimes(1));
    // Money is a string at the column's precision. A number here would round.
    expect(mockSet).toHaveBeenCalledWith('weekly', '250.00');
  });

  it('sends no target of any kind — only the period and the amount', async () => {
    // Structural scoping: the person is derived from the token server-side. A
    // component that sent an anchor would be the first step toward the authority
    // inversion §4.2 forbids, so the argument list is asserted exactly.
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.type(screen.getByTestId('person-cap-input'), '10.00');
    await user.click(screen.getByTestId('person-cap-save'));

    await waitFor(() => expect(mockSet).toHaveBeenCalledTimes(1));
    const args = mockSet.mock.calls[0];
    expect(args).toHaveLength(2);
    expect(JSON.stringify(args)).not.toMatch(/github:|person_anchor|user_id/);
  });

  it('never sends enforcement_mode', async () => {
    // Not client-settable while the person layer is informational (#4630). A
    // component that sent `hard` would be asking for a promise nothing keeps.
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.type(screen.getByTestId('person-cap-input'), '10.00');
    await user.click(screen.getByTestId('person-cap-save'));

    await waitFor(() => expect(mockSet).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(mockSet.mock.calls[0])).not.toContain('enforcement_mode');
  });

  it('rejects a zero amount client-side and does not call the API', async () => {
    // Zero is indistinguishable downstream from "no limit", so it is refused and
    // the user is pointed at Remove limit instead. The server rejects it too — this
    // is the affordance, not the authority.
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.type(screen.getByTestId('person-cap-input'), '0');
    await user.click(screen.getByTestId('person-cap-save'));

    expect(await screen.findByRole('alert')).toHaveTextContent(/greater than zero/i);
    expect(mockSet).not.toHaveBeenCalled();
  });

  it('rejects a non-numeric amount without calling the API', async () => {
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.type(screen.getByTestId('person-cap-input'), 'lots');
    await user.click(screen.getByTestId('person-cap-save'));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(mockSet).not.toHaveBeenCalled();
  });

  it('reports a failed save as unsaved, not as a new limit', async () => {
    // A failed write that looked like a success would leave the person believing a
    // limit is in force when nothing was stored.
    mockSet.mockRejectedValue({ error: 'service_unavailable', message: 'down' });
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    await user.type(screen.getByTestId('person-cap-input'), '10.00');
    await user.click(screen.getByTestId('person-cap-save'));

    expect(await screen.findByTestId('person-cap-save-error')).toHaveTextContent(/was not saved/i);
  });
});

describe('PersonSpendingLimit — removing a limit', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCap);
    mockSet.mockResolvedValue(mockPersonCap);
    mockDelete.mockResolvedValue(undefined);
  });

  it('removes via DELETE, never a PUT of 0', async () => {
    // `0` is a real ceiling of zero dollars. Using it to mean "no limit" is exactly
    // the conflation `cap_status` exists to prevent.
    const user = userEvent.setup();
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-remove')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-remove'));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('monthly'));
    expect(mockSet).not.toHaveBeenCalled();
  });
});

describe('PersonSpendingLimit — failure states', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSet.mockResolvedValue(mockPersonCap);
    mockDelete.mockResolvedValue(undefined);
  });

  it('reports a load failure as a failure, never as "no limit"', async () => {
    // An outage is the one moment we cannot know whether a limit exists, so the
    // uncapped copy would be a claim we cannot support.
    mockGet.mockRejectedValue({ error: 'service_unavailable', message: 'down' });
    renderControl();

    await waitFor(() => expect(screen.getByTestId('person-cap-error')).toBeInTheDocument());
    expect(screen.getByTestId('person-cap-error')).toHaveTextContent(/not a statement that you have no limit/i);
    expect(screen.queryByTestId('person-cap-uncapped')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });
});

describe('PersonSpendingLimit — period handling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(mockPersonCap);
    mockSet.mockResolvedValue(mockPersonCap);
    mockDelete.mockResolvedValue(undefined);
  });

  it('reads the limit for the period it was given', async () => {
    renderControl('daily');

    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('daily'));
  });

  it('labels the amount field with the period noun', async () => {
    // The period noun now appears on the editor's field label rather than beside a
    // figure (#4685 moved the figure to the Cloud spend tile). It still has to be
    // stated somewhere the author can see: a box that just says "USD" gives no clue
    // whether 250 is a daily or a monthly ceiling, and the two differ ~30x.
    const user = userEvent.setup();
    renderControl('daily');

    await waitFor(() => expect(screen.getByTestId('person-cap-edit')).toBeInTheDocument());
    await user.click(screen.getByTestId('person-cap-edit'));
    expect(screen.getByLabelText(/limit per day \(usd\)/i)).toBeInTheDocument();
  });
});

describe('validateCapAmount', () => {
  // Unit-level, because the rules mirror the server's and a silent divergence
  // would let the UI submit values that always 422.
  it.each(['1', '250', '250.5', '250.50', '0.01'])('accepts %s', (value) => {
    expect(validateCapAmount(value)).toBeNull();
  });

  it.each([
    ['', 'empty'],
    ['0', 'zero is not "no limit"'],
    ['0.00', 'zero is not "no limit"'],
    ['-5', 'negative has no meaning'],
    ['1.234', 'more precision than the column holds'],
    ['abc', 'not a number'],
    ['$250', 'currency symbol'],
  ])('rejects %s (%s)', (value) => {
    expect(validateCapAmount(value)).not.toBeNull();
  });
});
