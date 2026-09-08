/**
 * BedrockAccountRouting tests — Issue #4745 (#4692 · R4).
 *
 * This panel decides **whose AWS account is billed** for a population's model calls, so
 * the properties worth pinning are the ones whose failure modes are silent or actively
 * misleading. Five groups:
 *
 * 1. **The scope reaches the wire intact.** Every write is asserted on the ENDPOINT
 *    ARGUMENTS, not on rendered text. A rule aimed at a half-specified scope saves
 *    cleanly, reads back as a rule, and routes nobody — #4511 on the governance surface
 *    itself. The team form is the sharp case: it must carry BOTH ids, because a team id
 *    is unique only inside its own org (#4344).
 *
 * 2. **The screen never misreports who is in force.** An admin rule over somebody's own
 *    selection must say it overrides them (§1.4); a self-selected row must not look like
 *    an admin rule; an unusable destination must not look like a working one. Each is a
 *    reading that sends an operator to change the wrong row.
 *
 * 3. **A refusal is an outcome, not a crash.** The save runs a real assume-role probe and
 *    legitimately refuses (ruling 4a). The modal must stay open with the reason — closing
 *    on failure would leave the operator believing traffic had been rerouted when nothing
 *    was stored, the exact inversion the probe exists to prevent.
 *
 * 4. **An outage is not an empty table.** "The rules could not be read" rendering as "no
 *    rules exist" is what gets a duplicate rule authored over a live one.
 *
 * 5. **Removal and re-verify tell the truth about the ladder.** Removing a rule does not
 *    strand anybody (§1.2), and a failed verify does not delete rules (§4.4). Both
 *    natural readings are the wrong ones, so the copy is asserted.
 *
 * `mappingScopePath` is kept REAL throughout — stubbing the function that encodes the
 * scope would make every assertion in group 1 vacuous, proving only that a mock was
 * called.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BedrockAccountRouting } from '@/components/bedrock/BedrockAccountRouting';
import { ToastProvider } from '@/contexts/ToastContext';
import type { DestinationSummary, MappingSummary } from '@/types/bedrockRouting';

vi.mock('@/services/bedrockRouting', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/bedrockRouting')>();
  return {
    ...actual,
    listMappings: vi.fn(),
    setMapping: vi.fn(),
    deleteMapping: vi.fn(),
    getEffectiveMapping: vi.fn(),
    listDestinations: vi.fn(),
    registerDestination: vi.fn(),
    verifyDestination: vi.fn(),
  };
});

vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn(),
  getCognitoTeams: vi.fn(),
  listPlatformUsers: vi.fn(),
}));

import {
  listMappings,
  setMapping,
  deleteMapping,
  getEffectiveMapping,
  listDestinations,
  registerDestination,
  verifyDestination,
} from '@/services/bedrockRouting';
import { getOrganizations, getCognitoTeams, listPlatformUsers } from '@/services/admin';

const mockListMappings = listMappings as ReturnType<typeof vi.fn>;
const mockSetMapping = setMapping as ReturnType<typeof vi.fn>;
const mockDeleteMapping = deleteMapping as ReturnType<typeof vi.fn>;
const mockGetEffective = getEffectiveMapping as ReturnType<typeof vi.fn>;
const mockListDestinations = listDestinations as ReturnType<typeof vi.fn>;
const mockRegisterDestination = registerDestination as ReturnType<typeof vi.fn>;
const mockVerifyDestination = verifyDestination as ReturnType<typeof vi.fn>;
const mockGetOrgs = getOrganizations as ReturnType<typeof vi.fn>;
const mockGetTeams = getCognitoTeams as ReturnType<typeof vi.fn>;
const mockListPeople = listPlatformUsers as ReturnType<typeof vi.fn>;

const ACME_PROD: DestinationSummary = {
  id: 'dest-acme',
  account_id: '111122223333',
  label: 'acme-prod',
  region: 'us-east-1',
  source: 'org-linked',
  owner_org_id: 'acme',
  routing_capable: true,
  verified_at: '2026-09-01T00:00:00Z',
  usable_for_routing: true,
  reason: null,
  used_by: 2,
};

/** Another tenant's destination — never offerable to an acme rule (§4.2 requirement 1). */
const GLOBEX_PROD: DestinationSummary = {
  ...ACME_PROD,
  id: 'dest-globex',
  account_id: '444455556666',
  label: 'globex-prod',
  owner_org_id: 'globex',
  used_by: 0,
};

/** Registered platform-wide: no owning tenant, so any scope may name it. */
const SHARED_POOL: DestinationSummary = {
  ...ACME_PROD,
  id: 'dest-shared',
  account_id: '777788889999',
  label: 'shared-pool',
  source: 'admin-registered',
  owner_org_id: null,
  used_by: 0,
};

/** Verified once, broken now. Must stay visible: this is where an outage is diagnosed. */
const BROKEN: DestinationSummary = {
  ...ACME_PROD,
  id: 'dest-broken',
  account_id: '000011112222',
  label: 'legacy-acct',
  routing_capable: false,
  usable_for_routing: false,
  reason: 'role_missing_bedrock_permission',
  used_by: 1,
};

/** Registered but never verified — a stack the admin has not run yet. */
const PENDING: DestinationSummary = {
  ...ACME_PROD,
  id: 'dest-pending',
  account_id: '333344445555',
  label: 'new-acct',
  routing_capable: false,
  verified_at: null,
  usable_for_routing: false,
  reason: null,
  used_by: 0,
};

const ORG_RULE: MappingSummary = {
  id: 'map-org',
  scope_type: 'org',
  scope_id_org: 'acme',
  scope_id_team: null,
  scope_id_user: null,
  scope: 'org:acme',
  destination_id: 'dest-acme',
  destination_account_id: '111122223333',
  destination_label: 'acme-prod',
  destination_usable: true,
  source: 'platform_admin',
  updated_at: '2026-09-01T00:00:00Z',
};

const TEAM_RULE: MappingSummary = {
  ...ORG_RULE,
  id: 'map-team',
  scope_type: 'team',
  scope_id_team: 'platform-eng',
  scope: 'team:acme:platform-eng',
};

/** The person's own pick. This panel displays it; it does not author it. */
const SELF_RULE: MappingSummary = {
  ...ORG_RULE,
  id: 'map-self',
  scope_type: 'user',
  scope_id_org: null,
  scope_id_user: 'user-casey',
  scope: 'user:user-casey',
  source: 'self',
};

/** A rule whose destination has since broken — §4.4: NO MATCH, so traffic walks on. */
const RULE_ON_BROKEN: MappingSummary = {
  ...ORG_RULE,
  id: 'map-broken',
  scope_type: 'team',
  scope_id_team: 'data-science',
  scope: 'team:acme:data-science',
  destination_id: 'dest-broken',
  destination_account_id: '000011112222',
  destination_label: 'legacy-acct',
  destination_usable: false,
};

/**
 * The platform roster behind the person picker — Issue #4827.
 *
 * `id` is the canonical `users.id` and is deliberately UUID-shaped and unlike anything
 * in the label: the picker's whole job is submitting an id no operator could type, so an
 * assertion on the submitted value must be unable to pass by matching a display string.
 *
 * Casey spans two tenants' worth of recognisability (a GitHub login), Dana has none —
 * the email-onboarded population, which must still be selectable.
 */
const CASEY: { id: string; orgId: string; email: string; name: string | null; githubUsername: string | null } = {
  id: '48270000-0000-4000-8000-00000000ca5e',
  orgId: 'acme',
  email: 'casey@acme.example',
  name: 'Casey Ng',
  githubUsername: 'caseyng',
};

/** No GitHub identity: legitimate and permanent, so the picker must still offer them. */
const DANA = {
  id: '48270000-0000-4000-8000-00000000da4a',
  orgId: 'globex',
  email: 'dana@globex.example',
  name: 'Dana Fox',
  githubUsername: null,
};

function renderPanel() {
  return render(
    <ToastProvider>
      <BedrockAccountRouting />
    </ToastProvider>,
  );
}

/** Choose somebody in a person picker, by the canonical id the option carries. */
async function pickPerson(user: ReturnType<typeof userEvent.setup>, testId: string, personId: string) {
  const select = await screen.findByTestId(testId);
  await waitFor(() => expect(within(select).getAllByRole('option').length).toBeGreaterThan(1));
  await user.selectOptions(select, personId);
}

/** Open the Add-rule modal and pick a scope type. */
async function openAddRule(user: ReturnType<typeof userEvent.setup>, scopeType: 'org' | 'team' | 'user') {
  await user.click(screen.getByTestId('routing-add-rule'));
  await user.click(await screen.findByTestId(`routing-scope-${scopeType}`));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListMappings.mockResolvedValue([ORG_RULE, TEAM_RULE, SELF_RULE]);
  mockListDestinations.mockResolvedValue([ACME_PROD, GLOBEX_PROD, SHARED_POOL, BROKEN, PENDING]);
  mockGetOrgs.mockResolvedValue({
    items: [
      { id: 'acme', name: 'Acme Corp' },
      { id: 'globex', name: 'Globex' },
    ],
    total: 2,
    page: 1,
    pageSize: 50,
    hasMore: false,
  });
  mockGetTeams.mockResolvedValue({
    items: [{ groupName: 'platform-eng', description: null, createdAt: null, updatedAt: null }],
    total: 1,
    page: 1,
    pageSize: 50,
    hasMore: false,
  });
  mockListPeople.mockResolvedValue({ items: [CASEY, DANA], total: 2, page: 1, pageSize: 50, hasMore: false });
  mockSetMapping.mockResolvedValue(ORG_RULE);
  mockDeleteMapping.mockResolvedValue(undefined);
  mockVerifyDestination.mockResolvedValue({ destination: ACME_PROD, verified: true, reason: null });
});

// ---------------------------------------------------------------------------
// Group 1 — the scope reaches the wire intact
// ---------------------------------------------------------------------------

describe('the authored scope reaches the wire intact', () => {
  it('sends an org rule as the org scope and the chosen destination id', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    // Asserted on the arguments: the scope object and a DESTINATION ID. An account
    // number here would be ruling 4a violated on the wire — an account alone is
    // unusable without an assumable role in it.
    await waitFor(() =>
      expect(mockSetMapping).toHaveBeenCalledWith(expect.objectContaining({ scope_type: 'org', org: 'acme' }), 'dest-acme'),
    );
  });

  it('sends a team rule carrying BOTH the org and the team id', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(await screen.findByLabelText('Team'), 'platform-eng');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    // A team scope naming only the team could route an unrelated tenant's same-id
    // team — the #4344 collision class.
    await waitFor(() =>
      expect(mockSetMapping).toHaveBeenCalledWith(
        expect.objectContaining({ scope_type: 'team', org: 'acme', team: 'platform-eng' }),
        'dest-acme',
      ),
    );
  });

  it('sends a user rule as the user scope with no org id', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    await pickPerson(user, 'routing-rule-user', DANA.id);
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    await waitFor(() =>
      expect(mockSetMapping).toHaveBeenCalledWith(expect.objectContaining({ scope_type: 'user', user: DANA.id }), 'dest-acme'),
    );
  });

  it('cannot save a half-specified scope', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');

    // Org chosen, destination chosen, team NOT chosen. A rule stored against a
    // half-specified scope would route nobody while reading back as a rule.
    expect(screen.getByTestId('routing-rule-save')).toBeDisabled();
    await user.click(screen.getByTestId('routing-rule-save'));
    expect(mockSetMapping).not.toHaveBeenCalled();
  });

  it('offers only destinations a rule for that org may legitimately name', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');

    const options = within(screen.getByLabelText('Destination') as HTMLSelectElement)
      .getAllByRole('option')
      .map((o) => (o as HTMLOptionElement).value);

    expect(options).toContain('dest-acme'); // acme's own
    expect(options).toContain('dest-shared'); // platform-registered, no owner
    expect(options).not.toContain('dest-globex'); // another tenant's
    expect(options).not.toContain('dest-broken'); // verified once, unusable now
    expect(options).not.toContain('dest-pending'); // never verified
  });

  it('clears a destination picked for a different org when the org changes', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'globex');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-globex');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');

    // Otherwise the previous tenant's destination stays selected and submits — a
    // cross-tenant rule the server would refuse, authored from a screen that showed
    // it as valid.
    expect((screen.getByLabelText('Destination') as HTMLSelectElement).value).toBe('');
    expect(screen.getByTestId('routing-rule-save')).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Group 1b — the person rung can be authored without knowing an id (Issue #4827)
// ---------------------------------------------------------------------------

/**
 * The person picker.
 *
 * The defect was NOT a wrong write: the server always refused an unknown `users.id`.
 * It was that no operator could produce a right one, so the field read as broken. Three
 * things therefore have to hold, and each has a silent failure mode:
 *
 * - **What reaches the wire is the canonical id**, never the label an operator read.
 *   A picker that submitted an email or a GitHub login would store rules that read back
 *   correctly and govern nobody.
 * - **The label is recognisable**, i.e. the GitHub username when one is linked. A
 *   dropdown of UUIDs is the original defect wearing a different control.
 * - **The GitHub-less population is still selectable.** Dropping them would leave a
 *   valid target unpickable — the same dead end, narrower.
 */
describe('the person rung is authored from a picker, not a typed id', () => {
  it('submits the canonical user id, not anything shown in the label', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    await pickPerson(user, 'routing-rule-user', CASEY.id);
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    // Asserted on the ARGUMENT. The id is UUID-shaped and appears in no label, so this
    // cannot pass by coincidence with the email or the GitHub login beside it.
    await waitFor(() => expect(mockSetMapping).toHaveBeenCalledWith(expect.objectContaining({ scope_type: 'user', user: CASEY.id }), 'dest-acme'));
    const [scope] = mockSetMapping.mock.calls[0];
    expect(scope.user).not.toBe(CASEY.email);
    expect(scope.user).not.toBe(CASEY.githubUsername);
  });

  it('names people by their GitHub username so an operator can recognise them', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    const select = await screen.findByTestId('routing-rule-user');

    // The operator requirement: the GitHub login is how people are recognised. A
    // dropdown of ids would be the same unusable control in different clothing.
    const option = await within(select).findByRole('option', { name: /caseyng/ });
    expect(option).toHaveValue(CASEY.id);
  });

  it('offers a member with no linked GitHub account, and says so', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    const select = await screen.findByTestId('routing-rule-user');

    // Email onboarding is permanent and legitimate. Hiding these people would leave a
    // valid rule target with no way to select it; showing them with a blank column
    // instead invites reading the gap as a load failure.
    const option = await within(select).findByRole('option', { name: /no GitHub linked/ });
    expect(option).toHaveValue(DANA.id);
  });

  it('searches server-side rather than filtering a full roster in the browser', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    await user.type(await screen.findByTestId('routing-rule-user-search'), 'caseyng');

    // A client-side filter over an unpaginated fetch is what the endpoint's pagination
    // exists to avoid; the query must reach the server.
    await waitFor(() => expect(mockListPeople).toHaveBeenCalledWith(expect.objectContaining({ q: 'caseyng' })));
  });

  it('keeps the chosen person selected when a later search excludes them', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    await pickPerson(user, 'routing-rule-user', CASEY.id);

    // The next page of results does not contain Casey.
    mockListPeople.mockResolvedValue({ items: [DANA], total: 1, page: 1, pageSize: 50, hasMore: false });
    await user.type(screen.getByTestId('routing-rule-user-search'), 'dana');
    await waitFor(() => expect(mockListPeople).toHaveBeenCalledWith(expect.objectContaining({ q: 'dana' })));

    // Otherwise the selection silently empties, Save disables itself, and nothing on
    // screen says why the admin can no longer submit.
    await waitFor(() => expect((screen.getByTestId('routing-rule-user') as HTMLSelectElement).value).toBe(CASEY.id));
  });

  it('discloses that it is showing only part of a larger roster', async () => {
    const user = userEvent.setup();
    mockListPeople.mockResolvedValue({ items: [CASEY, DANA], total: 900, page: 1, pageSize: 50, hasMore: true });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');

    // A picker showing 2 of 900 in silence reads as the complete roster — the #4688
    // failure where members past the first page became unreachable with nothing on
    // screen saying so.
    expect(await screen.findByTestId('routing-rule-user-truncated')).toHaveTextContent(/900/);
  });

  it('says the roster could not be loaded rather than showing nobody', async () => {
    const user = userEvent.setup();
    mockListPeople.mockRejectedValue({ message: 'Service unavailable' });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');

    // "No matching person" during an outage reads as "this person does not exist",
    // which sends an admin to create a user who already has an account.
    const error = await screen.findByTestId('routing-rule-user-error');
    expect(error).toHaveTextContent('Service unavailable');
    expect(error).toHaveTextContent(/not a statement that the person does not exist/i);
    expect(screen.getByTestId('routing-rule-save')).toBeDisabled();
  });

  it('applies the same picker to the effective-mapping lookup', async () => {
    const user = userEvent.setup();
    mockGetEffective.mockResolvedValue({
      user_id: CASEY.id,
      rung: 'user',
      account_id: '111122223333',
      destination_id: 'dest-acme',
      destination_label: 'acme-prod',
      source: 'platform_admin',
      overrides_self_selection: false,
      shadowed_rung: null,
      shadowed_account_id: null,
    });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    // This field took a raw id too: an operator who cannot name a person cannot check
    // who serves them either.
    await pickPerson(user, 'routing-effective-user', CASEY.id);
    await user.click(screen.getByTestId('routing-effective-lookup'));

    await waitFor(() => expect(mockGetEffective).toHaveBeenCalledWith(CASEY.id));
  });

  it('will not look up or save until somebody is chosen', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    expect(screen.getByTestId('routing-effective-lookup')).toBeDisabled();

    await openAddRule(user, 'user');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    expect(screen.getByTestId('routing-rule-save')).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Group 2 — the screen never misreports who is in force
// ---------------------------------------------------------------------------

describe('the screen reports which rule is in force', () => {
  it('names the rung that produced a person’s destination', async () => {
    const user = userEvent.setup();
    mockGetEffective.mockResolvedValue({
      user_id: 'user-dana',
      rung: 'team',
      account_id: '111122223333',
      destination_id: 'dest-acme',
      destination_label: 'acme-prod',
      source: 'platform_admin',
      overrides_self_selection: false,
      shadowed_rung: 'org',
      shadowed_account_id: '777788889999',
    });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await pickPerson(user, 'routing-effective-user', DANA.id);
    await user.click(screen.getByTestId('routing-effective-lookup'));

    // The rung is the point, not decoration: an effective destination shown WITHOUT
    // its rung invites the reader to "fix" the wrong row.
    const result = await screen.findByTestId('routing-effective-result');
    expect(result).toHaveTextContent(/team/);
    expect(result).toHaveTextContent(/acme-prod/);
    expect(screen.getByTestId('routing-effective-shadowed')).toHaveTextContent(/org/);
  });

  it('says when an admin rule overrides the person’s own selection', async () => {
    const user = userEvent.setup();
    mockGetEffective.mockResolvedValue({
      user_id: 'user-casey',
      rung: 'user',
      account_id: '111122223333',
      destination_id: 'dest-acme',
      destination_label: 'acme-prod',
      source: 'platform_admin',
      overrides_self_selection: true,
      shadowed_rung: 'org',
      shadowed_account_id: '777788889999',
    });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await pickPerson(user, 'routing-effective-user', CASEY.id);
    await user.click(screen.getByTestId('routing-effective-lookup'));

    // §1.4 settled on "admin wins" AND required the UI to disclose it. Silence here
    // would show the person's own stale pick as active — #4511 one layer up.
    await waitFor(() => expect(screen.getByTestId('routing-effective-override')).toBeInTheDocument());
  });

  it('reports the platform default as an answer rather than an absence', async () => {
    const user = userEvent.setup();
    mockGetEffective.mockResolvedValue({
      user_id: 'user-nobody',
      rung: 'platform',
      account_id: null,
      destination_id: null,
      destination_label: null,
      source: null,
      overrides_self_selection: false,
      shadowed_rung: null,
      shadowed_account_id: null,
    });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await pickPerson(user, 'routing-effective-user', DANA.id);
    await user.click(screen.getByTestId('routing-effective-lookup'));

    expect(await screen.findByTestId('routing-effective-result')).toHaveTextContent(/platform account/);
  });

  it('leaves a failed lookup as an error, not a confident "platform"', async () => {
    const user = userEvent.setup();
    mockGetEffective.mockRejectedValue({ detail: { reason: 'scope_not_found', message: 'No such person.' } });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await pickPerson(user, 'routing-effective-user', CASEY.id);
    await user.click(screen.getByTestId('routing-effective-lookup'));

    // A failed lookup that resolved to "platform" would tell an admin this person has
    // no rule when they may well have one. Since #4827 the id is picker-sourced, so a
    // refusal here is a server-side or transport failure rather than a typo — which
    // makes rendering it as a confident answer worse, not better.
    expect(await screen.findByTestId('routing-effective-error')).toHaveTextContent('No such person.');
    expect(screen.queryByTestId('routing-effective-result')).not.toBeInTheDocument();
  });

  it('marks a self-selected row as the person’s own and offers no Remove', async () => {
    renderPanel();
    const row = await screen.findByTestId(`routing-mapping-${SELF_RULE.id}`);

    // This panel displays somebody's own choice; it does not author or delete it.
    // Rendering it identically to an admin rule would invite an admin to "remove"
    // a row this surface does not own.
    expect(within(row).getByTestId(`routing-managed-by-user-${SELF_RULE.id}`)).toBeInTheDocument();
    expect(within(row).queryByTestId(`routing-remove-${SELF_RULE.id}`)).not.toBeInTheDocument();
  });

  it('warns that a rule on an unusable destination falls through instead of failing', async () => {
    mockListMappings.mockResolvedValue([RULE_ON_BROKEN]);
    renderPanel();

    // §4.4: the resolver treats an unusable destination as NO MATCH and walks on. So
    // this rule is not broken-and-deletable — its traffic is being billed somewhere
    // ELSE, which is the fact the operator needs.
    expect(await screen.findByTestId(`routing-mapping-unusable-${RULE_ON_BROKEN.id}`)).toHaveTextContent(/fall through/i);
  });

  it('states the fail-closed behaviour without being asked', async () => {
    renderPanel();
    // Ruling 1 / §2.5. Surprising if undisclosed: an admin who assumes a fallback to
    // the platform account will not treat a broken destination as urgent.
    expect(await screen.findByTestId('routing-fail-closed-banner')).toHaveTextContent(/never silently falls back/i);
  });

  it('renders the platform default as a row with no controls', async () => {
    renderPanel();
    const row = await screen.findByTestId('routing-platform-row');

    // Rung 4 is the ABSENCE of a mapping (§1.2), so there is nothing to author here.
    // A Remove or Edit button would imply a record that does not exist.
    expect(row).toHaveTextContent(/everyone else/);
    expect(within(row).queryByRole('button')).not.toBeInTheDocument();
  });

  it('distinguishes never-verified from failed in the destinations table', async () => {
    renderPanel();

    // Two different jobs: run the CloudFormation stack, versus debug an IAM policy.
    // Collapsing them sends an operator to the wrong one.
    expect(await screen.findByTestId(`routing-dest-unverified-${PENDING.id}`)).toBeInTheDocument();
    const failed = screen.getByTestId(`routing-dest-failed-${BROKEN.id}`);
    expect(failed).toHaveTextContent(/bedrock:InvokeModel/); // the remediation, not just "failed"
  });

  it('keeps a failed destination visible rather than filtering it away', async () => {
    renderPanel();
    // The destinations table is where a fail-closed outage gets diagnosed; hiding the
    // broken row hides the only row worth acting on.
    expect(await screen.findByTestId(`routing-destination-${BROKEN.id}`)).toBeInTheDocument();
  });

  it('shows how many rules point at each destination', async () => {
    renderPanel();
    // Deleting or breaking a referenced destination is an outage (§8.3); the count is
    // what makes the blast radius visible before it happens.
    const row = await screen.findByTestId(`routing-destination-${ACME_PROD.id}`);
    expect(row).toHaveTextContent(/2 rules/);
  });
});

// ---------------------------------------------------------------------------
// Group 3 — a refusal is an outcome, not a crash
// ---------------------------------------------------------------------------

describe('a save-time refusal', () => {
  it('keeps the modal open and shows the reason', async () => {
    const user = userEvent.setup();
    mockSetMapping.mockRejectedValue({
      detail: { reason: 'role_missing_bedrock_permission', message: 'That role cannot invoke Bedrock.' },
    });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    // Closing on failure would leave the operator believing traffic had been rerouted
    // when nothing was stored — the inverse of the #4511 defect the probe prevents.
    expect(await screen.findByTestId('routing-rule-error')).toHaveTextContent('That role cannot invoke Bedrock.');
    expect(screen.getByTestId('routing-rule-save')).toBeInTheDocument();
    expect(screen.queryByTestId('routing-confirmation')).not.toBeInTheDocument();
  });

  it('does not refetch as though something had changed', async () => {
    const user = userEvent.setup();
    mockSetMapping.mockRejectedValue({ detail: { reason: 'account_unlinked', message: 'Not linked.' } });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalledTimes(1));

    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));
    await screen.findByTestId('routing-rule-error');

    // Nothing was stored, so a reload would only redraw the same table while
    // suggesting a write had landed.
    expect(mockListMappings).toHaveBeenCalledTimes(1);
  });

  it('warns before saving that these calls will fail rather than fall back', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());
    await openAddRule(user, 'org');

    // Stated at the moment the admin can still choose differently, not only in the
    // page banner.
    expect(screen.getByTestId('routing-fail-closed-warning')).toHaveTextContent(/do not fall back/i);
  });

  it('confirms a successful save and refetches both tables', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalledTimes(1));

    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    await waitFor(() => expect(mockListMappings).toHaveBeenCalledTimes(2));
    expect(mockListDestinations).toHaveBeenCalledTimes(2);
    // The propagation delay is disclosed: a request made immediately afterwards may
    // still use the previous account, which otherwise reads as the save not working.
    expect(screen.getByTestId('routing-confirmation')).toHaveTextContent(/within about a minute/i);
  });

  it('tells the admin a user-rung save overrides that person’s own choice', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'user');
    await pickPerson(user, 'routing-rule-user', CASEY.id);
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    // §1.4 again, at the write rather than the read: silently overriding somebody's
    // own selection is the surprise the ruling required disclosing.
    expect(await screen.findByTestId('routing-confirmation')).toHaveTextContent(/overrides whatever the person selected/i);
  });
});

// ---------------------------------------------------------------------------
// Group 4 — an outage is not an empty table
// ---------------------------------------------------------------------------

describe('a failed load', () => {
  it('says the rules could not be read instead of showing no rules', async () => {
    mockListMappings.mockRejectedValue({ message: 'Service unavailable' });
    renderPanel();

    // "No rules exist" during an outage is the reading that gets a duplicate rule
    // authored over a live one, or a rule "fixed" on the wrong rung.
    const error = await screen.findByTestId('routing-mappings-error');
    expect(error).toHaveTextContent('Service unavailable');
    expect(error).toHaveTextContent(/not a statement that no rules exist/i);
    expect(screen.queryByTestId('routing-platform-row')).not.toBeInTheDocument();
  });

  it('reports a destinations outage separately from the rules', async () => {
    mockListDestinations.mockRejectedValue({ message: 'Service unavailable' });
    renderPanel();

    // Independent fetches: a destinations outage must not blank the rules table, which
    // is still accurate.
    expect(await screen.findByTestId('routing-destinations-error')).toBeInTheDocument();
    expect(screen.getByTestId(`routing-mapping-${ORG_RULE.id}`)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Group 5 — removal and re-verify tell the truth about the ladder
// ---------------------------------------------------------------------------

describe('removing a rule', () => {
  it('says the principals fall back rather than stop working', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId(`routing-remove-${TEAM_RULE.id}`));

    // The natural reading — "their traffic will now fail" — is the wrong one. §1.2:
    // removal un-shadows the ladder beneath.
    expect(await screen.findByTestId('routing-remove-ladder')).toHaveTextContent(/fall back/i);
  });

  it('deletes by the scope of the row that was chosen', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId(`routing-remove-${TEAM_RULE.id}`));
    await user.click(screen.getByTestId('routing-remove-confirm'));

    // Both ids again: a delete addressed by team id alone could remove another
    // tenant's rung.
    await waitFor(() =>
      expect(mockDeleteMapping).toHaveBeenCalledWith(
        expect.objectContaining({ scope_type: 'team', org: 'acme', team: 'platform-eng' }),
      ),
    );
  });
});

describe('re-verifying a destination', () => {
  it('re-probes rather than replaying the stored verdict', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId(`routing-verify-${ACME_PROD.id}`));

    // §6.7 item 4: a pass is a statement about NOW. The admin is clicking precisely
    // because they doubt the stored verdict.
    await waitFor(() => expect(mockVerifyDestination).toHaveBeenCalledWith(ACME_PROD.id));
  });

  it('surfaces a negative verdict as its reason, not as a crash', async () => {
    const user = userEvent.setup();
    mockVerifyDestination.mockResolvedValue({
      destination: BROKEN,
      verified: false,
      reason: 'routing_probe_inconclusive',
    });
    renderPanel();
    await user.click(await screen.findByTestId(`routing-verify-${BROKEN.id}`));

    // "Not capable" is a verdict, not a transport failure, and `inconclusive`
    // specifically is not a permissions problem — telling the operator to debug IAM
    // would be wrong.
    await waitFor(() => expect(screen.getByText(/not a permissions failure/i)).toBeInTheDocument());
    expect(mockListMappings).toHaveBeenCalledTimes(2); // still refetches: verified_at changed
  });

  it('states that existing rules survive a failed verification', async () => {
    renderPanel();
    // §4.4: deleting an admin's rules on a transient probe failure would be a far
    // larger and irreversible action than they asked for.
    expect(await screen.findByTestId('routing-destinations-note')).toHaveTextContent(/left in place/i);
  });
});

// ---------------------------------------------------------------------------
// Registering a destination — §6.6, the shared component
// ---------------------------------------------------------------------------

describe('registering a new destination', () => {
  it('requires an owning org before it can launch', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId('routing-register-open'));

    await user.type(await screen.findByLabelText('Nickname *'), 'new-dest');
    await user.type(screen.getByLabelText('AWS Account ID *'), '123456789012');

    // Every destination this endpoint mints has an owning tenant, so the cross-tenant
    // scope check stays total (§4.2 requirement 2).
    expect(screen.getByTestId('routing-register-launch')).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('Link to organization'), 'acme');
    expect(screen.getByTestId('routing-register-launch')).toBeEnabled();
  });

  it('registers through the shared connect form and reports the account not yet usable', async () => {
    const user = userEvent.setup();
    mockRegisterDestination.mockResolvedValue({
      destination: PENDING,
      launch_url: 'https://console.aws.amazon.com/cloudformation/quickcreate?x=1',
    });
    vi.spyOn(window, 'open').mockImplementation(() => null);
    renderPanel();
    await user.click(await screen.findByTestId('routing-register-open'));

    await user.type(await screen.findByLabelText('Nickname *'), 'new-dest');
    await user.type(screen.getByLabelText('AWS Account ID *'), '123456789012');
    await user.selectOptions(screen.getByLabelText('Link to organization'), 'acme');
    await user.click(screen.getByTestId('routing-register-launch'));

    await waitFor(() =>
      expect(mockRegisterDestination).toHaveBeenCalledWith({
        source: 'new_account',
        account_id: '123456789012',
        label: 'new-dest',
        link_to_org_id: 'acme',
      }),
    );
    // The role does not exist in the destination account until the admin runs the
    // stack, so the form moves to its verify step rather than declaring success.
    expect(await screen.findByTestId('routing-register-verify')).toBeInTheDocument();
  });

  it('offers no role-name field, because the template has no such parameter', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId('routing-register-open'));
    await screen.findByLabelText('Nickname *');

    // The v2 template names the role `ADP-Agent-${Nickname}` and declares no
    // role-name parameter. A field here would configure nothing while appearing to —
    // the #4511 shape this whole issue exists to prevent.
    expect(screen.queryByLabelText('Role Name')).not.toBeInTheDocument();
  });
});
