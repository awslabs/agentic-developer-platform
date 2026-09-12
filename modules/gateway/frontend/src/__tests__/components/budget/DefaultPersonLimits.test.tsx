/**
 * DefaultPersonLimits tests — Issue #4691 (#4690 · D2).
 *
 * This panel authors rules that govern a POPULATION, so the properties worth pinning
 * are the ones whose failure modes are silent — a rule that stores cleanly and
 * governs nobody, or a screen that misreports which rule is in force. Four groups:
 *
 * 1. **The scope reaches the wire intact.** Every scope form is asserted on the ENDPOINT
 *    ARGUMENTS, not on rendered text: a rule aimed at a mistyped or half-specified
 *    scope stores fine, reads back "capped", and bounds nobody (#4511 on the
 *    governance surface itself). The team form is the sharp case — it must carry BOTH
 *    ids, because a team id is unique only inside its own org.
 *
 * 2. **"No rule" is never a zero, and an outage is neither.** The three states are
 *    distinct claims and the panel must not collapse them: `$0.00` would read as
 *    "nobody here may spend anything", and "No rule set" during an outage is what gets
 *    a duplicate rule authored over a live one.
 *
 * 3. **Removal tells the truth about the ladder.** Deleting a rule does not uncap
 *    people a broader rung still covers — the natural reading is the wrong one, so the
 *    confirmation is asserted to say so.
 *
 * 4. **The route-absence twin.** The self-service write routes are gone server-side;
 *    these tests pin that no service function and no mock handler resurrects them.
 *    That last one is the grep the issue asks for: a mock for a deleted route makes a
 *    reintroduced editor pass its tests against a server that can only 405.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DefaultPersonLimits } from '@/components/budget/DefaultPersonLimits';
import { ToastProvider } from '@/contexts/ToastContext';
import { mockPersonDefaultFor } from '@/mocks/data/budgetSpend';

vi.mock('@/services/personCap', async (importOriginal) => {
  // `personDefaultScopePath` is kept REAL: it is the function that encodes the scope,
  // so stubbing it would make the scope assertions below vacuous — they would prove
  // the component called a mock, not that the right path was built. Only the three
  // network functions are replaced.
  const actual = await importOriginal<typeof import('@/services/personCap')>();
  return {
    ...actual,
    getPersonDefault: vi.fn(),
    setPersonDefault: vi.fn(),
    deletePersonDefault: vi.fn(),
  };
});

// Issue #4948: the team picker was mixing sources — orgs from the platform's tenancy
// tables, teams from Cognito groups. The scope this panel writes is matched against
// `users.team_id`, so a Cognito group NAME stored a rule that governs nobody.
vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn(),
  getOrgTeams: vi.fn(),
}));

import { getPersonDefault, setPersonDefault, deletePersonDefault } from '@/services/personCap';
import { getOrganizations, getOrgTeams } from '@/services/admin';

const mockGetDefault = getPersonDefault as ReturnType<typeof vi.fn>;
const mockSetDefault = setPersonDefault as ReturnType<typeof vi.fn>;
const mockDeleteDefault = deletePersonDefault as ReturnType<typeof vi.fn>;
const mockGetOrgs = getOrganizations as ReturnType<typeof vi.fn>;
const mockGetTeams = getOrgTeams as ReturnType<typeof vi.fn>;

function renderPanel() {
  return render(
    <ToastProvider>
      <DefaultPersonLimits />
    </ToastProvider>,
  );
}

/** The platform rung answers `capped`; anything else answers `uncapped`. */
function defaultForScope(scope: { scope_type: string; org?: string; team?: string }, period: string) {
  const segment =
    scope.scope_type === 'platform' ? 'platform' : scope.scope_type === 'org' ? `org:${scope.org}` : `team:${scope.org}:${scope.team}`;
  return mockPersonDefaultFor(segment, period);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetDefault.mockImplementation((scope, period) => Promise.resolve(defaultForScope(scope, period)));
  mockSetDefault.mockResolvedValue(mockPersonDefaultFor('platform', 'monthly'));
  mockDeleteDefault.mockResolvedValue(undefined);
  mockGetOrgs.mockResolvedValue({ items: [{ id: 'org-acme', name: 'Acme' }], total: 1, page: 1, pageSize: 50, hasMore: false });
  mockGetTeams.mockResolvedValue({
    items: [{ id: 'team-7f3a', name: 'platform-eng', departmentId: 'dept-1', orgId: 'org-acme' }],
    total: 1,
    page: 1,
    pageSize: 50,
    hasMore: false,
  });
});

describe('DefaultPersonLimits — the platform rung', () => {
  it('reads all three periods for the platform scope on mount', async () => {
    renderPanel();

    await waitFor(() => expect(mockGetDefault).toHaveBeenCalledTimes(3));

    // The scope object, not a path string: the component addresses scopes structurally
    // and `personDefaultScopePath` (real, not stubbed) does the encoding.
    const periods = mockGetDefault.mock.calls.map(([, period]) => period).sort();
    expect(periods).toEqual(['daily', 'monthly', 'weekly']);
    for (const [scope] of mockGetDefault.mock.calls) {
      expect(scope).toEqual({ scope_type: 'platform', org: undefined, team: undefined });
    }
  });

  it('renders the authored platform amount, not a bare number with no rule behind it', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));
    expect(screen.getAllByTestId('person-default-amount')[0].textContent).toContain('1,000.00');
  });

  it('offers Remove only where a rule exists', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));
    // Three platform rows, all capped in the fixture -> three Remove buttons.
    expect(screen.getAllByRole('button', { name: /^Remove$/ }).length).toBe(3);
  });
});

describe('DefaultPersonLimits — scope forms reach the wire intact (#4511)', () => {
  it('an org scope is looked up with its org id', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockGetDefault).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(mockGetOrgs).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText('Scope'), 'org');
    await user.selectOptions(screen.getByLabelText('GitHub org'), 'org-acme');
    await user.click(screen.getByTestId('person-default-inspect'));

    await waitFor(() => expect(mockGetDefault).toHaveBeenCalledWith({ scope_type: 'org', org: 'org-acme', team: undefined }, expect.any(String)));
  });

  it('a team scope carries BOTH ids — a team id is unique only inside its org', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockGetOrgs).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText('Scope'), 'team');
    await user.selectOptions(screen.getByLabelText('GitHub org'), 'org-acme');
    await waitFor(() => expect(mockGetTeams).toHaveBeenCalledWith('org-acme', expect.anything()));
    await user.selectOptions(screen.getByLabelText('Team'), 'team-7f3a');
    await user.click(screen.getByTestId('person-default-inspect'));

    await waitFor(() =>
      // `teams.id`, not the team's name (#4948): this scope is matched against
      // `users.team_id`, so a name here is a rule nobody is ever inside.
      expect(mockGetDefault).toHaveBeenCalledWith({ scope_type: 'team', org: 'org-acme', team: 'team-7f3a' }, expect.any(String)),
    );
  });

  it('cannot look up an org scope with no org picked — the half-specified rule is unreachable', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockGetOrgs).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText('Scope'), 'org');
    expect(screen.getByTestId('person-default-inspect')).toBeDisabled();
  });

  it('changing the org clears the team — a team id from another org is the cross-tenant mistake', async () => {
    const user = userEvent.setup();
    mockGetOrgs.mockResolvedValue({
      items: [
        { id: 'org-acme', name: 'Acme' },
        { id: 'org-other', name: 'Other' },
      ],
      total: 2,
      page: 1,
      pageSize: 50,
      hasMore: false,
    });
    renderPanel();
    await waitFor(() => expect(mockGetOrgs).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText('Scope'), 'team');
    await user.selectOptions(screen.getByLabelText('GitHub org'), 'org-acme');
    await waitFor(() => expect(mockGetTeams).toHaveBeenCalledWith('org-acme', expect.anything()));
    await user.selectOptions(screen.getByLabelText('Team'), 'team-7f3a');

    await user.selectOptions(screen.getByLabelText('GitHub org'), 'org-other');

    // Team cleared, so the scope is incomplete and cannot be submitted.
    await waitFor(() => expect(screen.getByTestId('person-default-inspect')).toBeDisabled());
  });
});

describe('DefaultPersonLimits — authoring', () => {
  it('writes the amount at 2dp to the scope and period of the row that opened the modal', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-edit-platform||-weekly'));
    const input = screen.getByTestId('person-default-amount-input');
    await user.clear(input);
    await user.type(input, '1500');
    await user.click(screen.getByTestId('person-default-save'));

    await waitFor(() =>
      expect(mockSetDefault).toHaveBeenCalledWith({ scope_type: 'platform', org: undefined, team: undefined }, 'weekly', '1500.00'),
    );
  });

  it('rejects 0 and says to use Remove — 0 is a real ceiling of zero dollars', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-edit-platform||-monthly'));
    const input = screen.getByTestId('person-default-amount-input');
    await user.clear(input);
    await user.type(input, '0');
    await user.click(screen.getByTestId('person-default-save'));

    expect(await screen.findByText(/must be greater than 0/i)).toBeInTheDocument();
    expect(mockSetDefault).not.toHaveBeenCalled();
  });

  it('rejects more than 2 decimal places rather than letting the server 422 it', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-edit-platform||-monthly'));
    const input = screen.getByTestId('person-default-amount-input');
    await user.clear(input);
    await user.type(input, '10.123');
    await user.click(screen.getByTestId('person-default-save'));

    expect(await screen.findByText(/at most 2 decimal places/i)).toBeInTheDocument();
    expect(mockSetDefault).not.toHaveBeenCalled();
  });

  it('a failed write keeps the modal open — a closed modal would imply the rule was stored', async () => {
    const user = userEvent.setup();
    mockSetDefault.mockRejectedValue(new Error('backend unavailable'));
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-edit-platform||-monthly'));
    const input = screen.getByTestId('person-default-amount-input');
    await user.clear(input);
    await user.type(input, '500');
    await user.click(screen.getByTestId('person-default-save'));

    // The failure surfaces on the FIELD (`aria-invalid` + the inline error), not only
    // as a toast — a toast that has already faded leaves no evidence the write failed.
    // Asserted via the input rather than the message text, which the toast also
    // renders, so this cannot pass on the toast alone.
    await waitFor(() => expect(screen.getByTestId('person-default-amount-input')).toHaveAttribute('aria-invalid', 'true'));
    // The modal is still open: closing it would imply a population is now bounded.
    expect(screen.getByTestId('person-default-save')).toBeInTheDocument();
  });

  it('confirms the write persistently and states the enforcement delay', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-edit-platform||-monthly'));
    const input = screen.getByTestId('person-default-amount-input');
    await user.clear(input);
    await user.type(input, '1000');
    await user.click(screen.getByTestId('person-default-save'));

    // Persistent (an Alert, not a toast): these rules appear in no scannable list, so
    // a vanishing confirmation reads as a write that did not land.
    const confirmation = await screen.findByTestId('person-default-confirmation');
    // The TTL caveat: enforcement picks new rules up within ~60s, and the first minute
    // is exactly when an operator tests whether the rule works.
    expect(confirmation.textContent).toMatch(/within about a minute/i);
    expect(confirmation.textContent).toMatch(/future members/i);
  });
});

describe('DefaultPersonLimits — removal tells the truth about the ladder', () => {
  it('warns that removal may not uncap anybody, then deletes the right scope and period', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-remove-platform||-daily'));

    // The natural reading of "remove the limit" is "these people are now unlimited",
    // and wherever a broader rung exists that is false.
    const ladder = await screen.findByTestId('person-default-remove-ladder');
    expect(ladder.textContent).toMatch(/does not uncap/i);
    expect(ladder.textContent).toMatch(/next rule up/i);

    await user.click(screen.getByTestId('person-default-remove-confirm'));

    await waitFor(() => expect(mockDeleteDefault).toHaveBeenCalledWith({ scope_type: 'platform', org: undefined, team: undefined }, 'daily'));
  });
});

describe('DefaultPersonLimits — "no rule", zero, and an outage are three different claims', () => {
  it('an unauthored scope says "No rule set", never $0.00', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockGetOrgs).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText('Scope'), 'org');
    await user.selectOptions(screen.getByLabelText('GitHub org'), 'org-acme');
    await user.click(screen.getByTestId('person-default-inspect'));

    const orgSection = await screen.findByTestId('person-default-scope-org|org-acme|');
    await waitFor(() => expect(within(orgSection).getAllByTestId('person-default-amount-none').length).toBe(3));
    expect(within(orgSection).queryByText(/\$0\.00/)).not.toBeInTheDocument();
  });

  it('an unauthored scope offers no Remove — there is nothing to remove', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockGetOrgs).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText('Scope'), 'org');
    await user.selectOptions(screen.getByLabelText('GitHub org'), 'org-acme');
    await user.click(screen.getByTestId('person-default-inspect'));

    const orgSection = await screen.findByTestId('person-default-scope-org|org-acme|');
    await waitFor(() => expect(within(orgSection).getAllByTestId('person-default-amount-none').length).toBe(3));
    expect(within(orgSection).queryByRole('button', { name: /^Remove$/ })).not.toBeInTheDocument();
  });

  it('a read failure is NOT rendered as "No rule set"', async () => {
    mockGetDefault.mockRejectedValue(new Error('table unreachable'));
    renderPanel();

    await waitFor(() => expect(screen.getAllByTestId('person-default-amount-error').length).toBe(3));
    // The reading that would get a duplicate rule authored over a live one.
    expect(screen.queryByTestId('person-default-amount-none')).not.toBeInTheDocument();
    expect(screen.queryByTestId('person-default-amount')).not.toBeInTheDocument();
  });
});

describe('DefaultPersonLimits — the panel does not imply a complete inventory', () => {
  it('says a scope not shown has not been checked, which is not the same as having no rule', async () => {
    renderPanel();
    const note = await screen.findByTestId('person-default-not-a-list');
    expect(note.textContent).toMatch(/not a complete list/i);
    expect(note.textContent).toMatch(/has not been checked/i);
  });
});

describe('DefaultPersonLimits — no mode picker (defaults are always hard)', () => {
  it('offers no enforcement-mode control: the only reachable value is the one in force', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getAllByTestId('person-default-amount').length).toBe(3));

    await user.click(screen.getByTestId('person-default-edit-platform||-monthly'));

    expect(screen.queryByLabelText(/enforcement/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/mode/i)).not.toBeInTheDocument();
    // And nothing offers the informational option that `PersonSpendingLimit` still
    // renders for legacy individual rows — a soft default is a shape the API rejects.
    expect(screen.queryByText(/informational/i)).not.toBeInTheDocument();
  });
});

/**
 * The route-absence twin of `PersonSpendingLimit.test.tsx` — Issue #4691.
 *
 * The self-service `PUT`/`DELETE /me/budget/person-cap` routes were deleted by the
 * #4690 ruling. These two tests are the grep the issue asks for, and the mock-handler
 * one is the gap #4696 left: a handler for a deleted route makes a reintroduced
 * editor pass its tests in mock mode against a server that can only 405, so the mock
 * would be the only reason the feature looked like it worked.
 */
describe('the removed self-service writes stay removed', () => {
  it('no service function references the removed write routes', async () => {
    const services = await import('@/services/personCap');

    expect('setMyPersonCap' in services).toBe(false);
    expect('deleteMyPersonCap' in services).toBe(false);

    // Nothing in the module builds a write against the self path either — a wrapper
    // under a different name would defeat the two checks above.
    const source = Object.values(services)
      .filter((v): v is (...args: never[]) => unknown => typeof v === 'function')
      .map((fn) => fn.toString())
      .join('\n');
    expect(source).not.toMatch(/put[^\n]*\/me\/budget\/person-cap/);
    expect(source).not.toMatch(/delete[^\n]*\/me\/budget\/person-cap/);
  });

  it('no mock handler offers the removed write routes', async () => {
    const { budgetHandlers } = await import('@/mocks/handlers/budget');

    const selfCapWrites = budgetHandlers.filter((h) => {
      const { method, path } = h.info as { method: string; path: string };
      return String(path).includes('/me/budget/person-cap') && ['PUT', 'DELETE'].includes(String(method).toUpperCase());
    });

    expect(selfCapWrites).toHaveLength(0);

    // The self READ is the surface that remains, so its handler must still be there —
    // this assertion is what stops the test above from passing by deleting all of them.
    const selfCapReads = budgetHandlers.filter((h) => {
      const { method, path } = h.info as { method: string; path: string };
      return String(path).includes('/me/budget/person-cap') && String(method).toUpperCase() === 'GET';
    });
    expect(selfCapReads).toHaveLength(1);
  });
});
