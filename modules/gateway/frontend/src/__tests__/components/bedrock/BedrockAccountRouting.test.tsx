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
import type { Team } from '@/types';

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
    getDestinationSetup: vi.fn(),
    verifyDestination: vi.fn(),
    listExistingAwsConnections: vi.fn(),
    linkAwsConnection: vi.fn(),
    unlinkAwsConnection: vi.fn(),
  };
});

vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn(),
  getOrgTeams: vi.fn(),
  listPlatformUsers: vi.fn(),
}));

import {
  listMappings,
  setMapping,
  deleteMapping,
  getEffectiveMapping,
  listDestinations,
  registerDestination,
  getDestinationSetup,
  verifyDestination,
  listExistingAwsConnections,
  linkAwsConnection,
  unlinkAwsConnection,
} from '@/services/bedrockRouting';
import { getOrganizations, getOrgTeams, listPlatformUsers } from '@/services/admin';

const mockListMappings = listMappings as ReturnType<typeof vi.fn>;
const mockSetMapping = setMapping as ReturnType<typeof vi.fn>;
const mockDeleteMapping = deleteMapping as ReturnType<typeof vi.fn>;
const mockGetEffective = getEffectiveMapping as ReturnType<typeof vi.fn>;
const mockListDestinations = listDestinations as ReturnType<typeof vi.fn>;
const mockRegisterDestination = registerDestination as ReturnType<typeof vi.fn>;
const mockVerifyDestination = verifyDestination as ReturnType<typeof vi.fn>;
const mockGetOrgs = getOrganizations as ReturnType<typeof vi.fn>;
const mockGetTeams = getOrgTeams as ReturnType<typeof vi.fn>;
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

/**
 * The org's teams as the TENANCY MODEL returns them — Issue #4947.
 *
 * `id` is deliberately UUID-shaped and shares no substring with `name`, because the whole
 * defect class here is an id/label confusion: the resolver matches `scope_id_team` against
 * `users.team_id`, which is a `teams.id`. A picker submitting the display name would store
 * a rule that reads back as configured routing and fires for nobody, and an assertion that
 * could pass by matching either string would not notice.
 */
const APP_DEV: Team = {
  id: '49470000-0000-4000-8000-0000000000a1',
  departmentId: 'dept-eng',
  name: 'App-Dev',
  createdAt: '2026-09-01T00:00:00Z',
};

const PLATFORM_ADMIN_TEAM: Team = {
  id: '49470000-0000-4000-8000-0000000000b2',
  departmentId: 'dept-eng',
  name: 'Platform-admin',
  createdAt: '2026-09-01T00:00:00Z',
};

const TEAM_RULE: MappingSummary = {
  ...ORG_RULE,
  id: 'map-team',
  scope_type: 'team',
  scope_id_team: APP_DEV.id,
  scope: `team:acme:${APP_DEV.id}`,
};

/**
 * A rule authored before the panel read the tenancy model: `scope_id_team` holds a Cognito
 * group NAME, which names no `teams.id` and so can never match a request. It must stay on
 * screen and be flagged — hiding it would leave an admin unable to re-author the only rule
 * they need to fix.
 */
const LEGACY_COGNITO_TEAM_RULE: MappingSummary = {
  ...ORG_RULE,
  id: 'map-legacy-team',
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
  scope_id_team: PLATFORM_ADMIN_TEAM.id,
  scope: `team:acme:${PLATFORM_ADMIN_TEAM.id}`,
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
    items: [APP_DEV, PLATFORM_ADMIN_TEAM],
    total: 2,
    page: 1,
    pageSize: 100,
    hasMore: false,
  });
  mockListPeople.mockResolvedValue({ items: [CASEY, DANA], total: 2, page: 1, pageSize: 50, hasMore: false });
  mockSetMapping.mockResolvedValue(ORG_RULE);
  mockDeleteMapping.mockResolvedValue(undefined);
  mockVerifyDestination.mockResolvedValue({ destination: ACME_PROD, verified: true, reason: null });
  vi.mocked(getDestinationSetup).mockResolvedValue({
    account_id: PENDING.account_id,
    role_arn: `arn:aws:iam::${PENDING.account_id}:role/ADP-Agent-${PENDING.label}`,
    region: 'us-east-1',
    launch_url: 'https://console.aws.amazon.com/cloudformation/quickcreate?fresh=1',
    download_filename: 'adp-bedrock.zip',
    download_base64: btoa('portable-cloudformation-package'),
  });
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
    await user.selectOptions(await screen.findByLabelText('Team'), APP_DEV.id);
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    // A team scope naming only the team could route an unrelated tenant's same-id
    // team — the #4344 collision class.
    await waitFor(() =>
      expect(mockSetMapping).toHaveBeenCalledWith(
        expect.objectContaining({ scope_type: 'team', org: 'acme', team: APP_DEV.id }),
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
// Group 1c — the team rung is sourced from the tenancy model (Issue #4947)
// ---------------------------------------------------------------------------

/**
 * The team picker's SOURCE, and the id namespace it submits.
 *
 * The defect: teams came from `getCognitoTeams` — a list derived by scanning the Cognito
 * user pool for distinct `custom:team_id` *values* — so an org whose teams live in the
 * tenancy tables offered nothing at all and its team rung could not be authored. Three
 * properties, each with a silent failure mode:
 *
 * - **The source is the tenancy endpoint.** Asserted on the CALL, because a picker
 *   populated from the wrong list is empty rather than wrong, and an empty dropdown reads
 *   as "this org has no teams".
 * - **What reaches the wire is `teams.id`**, the namespace the resolver compares against
 *   (`custom:team_id` ← `users.team_id` ← primary `teams.id`). A rule storing a display
 *   name reads back as configured routing and matches no request — #4511 on the surface
 *   that decides whose bill pays. The fixtures' ids share no substring with their names so
 *   this assertion cannot pass by coincidence.
 * - **Nothing is silently absent.** A truncated list, a failed read, and a legacy rule
 *   whose id no longer resolves each get said out loud; each one's natural silent
 *   rendering is a different wrong conclusion for the admin.
 */
describe('the team rung is sourced from the tenancy model', () => {
  it('populates the team picker from the org-wide tenancy list, scoped to the chosen org', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');

    // The endpoint, and the org. A team list read for the wrong org is the #4344
    // cross-tenant mistake with a friendly dropdown in front of it.
    await waitFor(() => expect(mockGetTeams).toHaveBeenCalledWith('acme', expect.objectContaining({ pageSize: 100 })));

    const options = within(await screen.findByLabelText('Team'))
      .getAllByRole('option')
      .map((o) => (o as HTMLOptionElement).value);
    expect(options).toContain(APP_DEV.id);
    expect(options).toContain(PLATFORM_ADMIN_TEAM.id);
  });

  it('labels a team by name while submitting its teams.id', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');

    // Recognisable label, canonical value — the `PersonPicker` contract one rung up.
    const option = await within(await screen.findByLabelText('Team')).findByRole('option', { name: 'App-Dev' });
    expect(option).toHaveValue(APP_DEV.id);

    await user.selectOptions(screen.getByLabelText('Team'), APP_DEV.id);
    await user.selectOptions(screen.getByLabelText('Destination'), 'dest-acme');
    await user.click(screen.getByTestId('routing-rule-save'));

    await waitFor(() => expect(mockSetMapping).toHaveBeenCalled());
    const [scope] = mockSetMapping.mock.calls[0];
    // The id namespace the resolver matches — NOT the name the operator read. This is
    // the assertion the defect class turns on in either direction.
    expect(scope.team).toBe(APP_DEV.id);
    expect(scope.team).not.toBe(APP_DEV.name);
  });

  it('re-reads the team list for the new org and drops the previous org’s team', async () => {
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(await screen.findByLabelText('Team'), APP_DEV.id);
    await user.selectOptions(screen.getByLabelText('Organization'), 'globex');

    // A `teams.id` is unique only inside its org, so a team surviving an org switch
    // could author a rule against another tenant's row (#4344).
    await waitFor(() => expect(mockGetTeams).toHaveBeenCalledWith('globex', expect.anything()));
    expect((screen.getByLabelText('Team') as HTMLSelectElement).value).toBe('');
    expect(screen.getByTestId('routing-rule-save')).toBeDisabled();
  });

  it('does not ask for teams until an organization is chosen', async () => {
    const user = userEvent.setup();
    // No team-scoped rule on screen, so the rules table drives no team read of its own
    // and any call here would have come from the form.
    mockListMappings.mockResolvedValue([ORG_RULE]);
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');

    // The endpoint is org-scoped; there is no org-less form of it to call. The control
    // says "select an organization first" rather than sitting empty and unexplained.
    expect(mockGetTeams).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Team')).toBeDisabled();
    expect(screen.getByText('Select an organization first')).toBeInTheDocument();
  });

  it('says the teams could not be read rather than showing an org with no teams', async () => {
    const user = userEvent.setup();
    mockGetTeams.mockRejectedValue({ message: 'Service unavailable' });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');

    // An empty dropdown during an outage reads as "this org has no teams", which sends
    // an admin to create teams that already exist — the `PersonPicker` lesson, one rung
    // up. This is also the failure the original defect wore: silence.
    const error = await screen.findByTestId('routing-rule-teams-error');
    expect(error).toHaveTextContent('Service unavailable');
    expect(error).toHaveTextContent(/not a statement that the organization has no teams/i);
  });

  it('discloses a truncated team list', async () => {
    const user = userEvent.setup();
    mockGetTeams.mockResolvedValue({ items: [APP_DEV], total: 130, page: 1, pageSize: 100, hasMore: true });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');

    // One page is what the read fetches. Silence would present it as the complete set.
    expect(await screen.findByTestId('routing-rule-teams-truncated')).toBeInTheDocument();
  });

  it('discloses a truncated organization list', async () => {
    const user = userEvent.setup();
    mockGetOrgs.mockResolvedValue({
      items: [{ id: 'acme', name: 'Acme Corp' }],
      total: 140,
      page: 1,
      pageSize: 100,
      hasMore: true,
    });
    renderPanel();
    await waitFor(() => expect(mockListMappings).toHaveBeenCalled());

    await openAddRule(user, 'team');

    // #4914's caveat: the org read is page 1 only. An admin whose org is on page 2 must
    // be told the list is cut, not left to conclude their org was never created.
    expect(await screen.findByTestId('routing-orgs-truncated')).toBeInTheDocument();
  });

  it('renders a team rule by its team NAME', async () => {
    renderPanel();

    // The stored id is unrecognisable on its own; the row exists to be read.
    const row = await screen.findByTestId(`routing-mapping-${TEAM_RULE.id}`);
    await waitFor(() => expect(row).toHaveTextContent('App-Dev'));
    expect(screen.queryByTestId(`routing-mapping-team-not-found-${TEAM_RULE.id}`)).not.toBeInTheDocument();
  });

  it('keeps a legacy rule whose team id no longer resolves, flagged rather than hidden', async () => {
    mockListMappings.mockResolvedValue([ORG_RULE, TEAM_RULE, LEGACY_COGNITO_TEAM_RULE]);
    renderPanel();

    // The pre-tenancy rule holds a Cognito group name where a `teams.id` belongs, so no
    // request can match it. Hiding it would leave the admin unable to find and re-author
    // the one rule that needs fixing; showing it unflagged would present dead routing as
    // live. Both readings are wrong in the expensive direction.
    const row = await screen.findByTestId(`routing-mapping-${LEGACY_COGNITO_TEAM_RULE.id}`);
    expect(row).toHaveTextContent('platform-eng');
    expect(await screen.findByTestId(`routing-mapping-team-not-found-${LEGACY_COGNITO_TEAM_RULE.id}`)).toHaveTextContent(/matches nobody/i);
  });

  it('withholds the “team not found” flag when the team list could not be read', async () => {
    mockListMappings.mockResolvedValue([LEGACY_COGNITO_TEAM_RULE]);
    mockGetTeams.mockRejectedValue({ message: 'Service unavailable' });
    renderPanel();

    // "We could not ask" is not "that team does not exist". Flagging on a failed read
    // would tell an admin to re-author a rule that may be perfectly correct.
    const row = await screen.findByTestId(`routing-mapping-${LEGACY_COGNITO_TEAM_RULE.id}`);
    expect(row).toHaveTextContent('platform-eng');
    await waitFor(() => expect(mockGetTeams).toHaveBeenCalled());
    expect(screen.queryByTestId(`routing-mapping-team-not-found-${LEGACY_COGNITO_TEAM_RULE.id}`)).not.toBeInTheDocument();
  });

  it('withholds the flag when the team list was truncated', async () => {
    mockListMappings.mockResolvedValue([LEGACY_COGNITO_TEAM_RULE]);
    mockGetTeams.mockResolvedValue({ items: [APP_DEV], total: 130, page: 1, pageSize: 100, hasMore: true });
    renderPanel();

    // The unresolved id may simply be on page 2. Same reasoning as the failed read: the
    // flag is a claim about a complete comparison.
    await waitFor(() => expect(mockGetTeams).toHaveBeenCalled());
    expect(await screen.findByTestId(`routing-mapping-${LEGACY_COGNITO_TEAM_RULE.id}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`routing-mapping-team-not-found-${LEGACY_COGNITO_TEAM_RULE.id}`)).not.toBeInTheDocument();
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
        expect.objectContaining({ scope_type: 'team', org: 'acme', team: APP_DEV.id }),
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
  it('downloads for an AWS administrator and restores the team rule only after verification', async () => {
    const user = userEvent.setup();
    mockListDestinations.mockResolvedValue([]);
    mockRegisterDestination.mockImplementation(async () => {
      mockListDestinations.mockResolvedValue([PENDING]);
      return { destination: PENDING, launch_url: 'https://console.aws.amazon.com/' };
    });
    const verified = { ...PENDING, routing_capable: true, usable_for_routing: true, verified_at: '2026-09-15T12:00:00Z' };
    mockVerifyDestination.mockResolvedValueOnce({ destination: PENDING, verified: false, reason: 'role_missing_bedrock_permission' });
    mockVerifyDestination.mockImplementationOnce(async () => {
      mockListDestinations.mockResolvedValue([verified]);
      return { destination: verified, verified: true, reason: null };
    });
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    URL.createObjectURL = vi.fn(() => 'blob:download');
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    renderPanel();
    await openAddRule(user, 'team');
    await user.selectOptions(screen.getByLabelText('Organization'), 'acme');
    await user.selectOptions(await screen.findByLabelText('Team'), APP_DEV.id);
    await user.click(screen.getByRole('button', { name: 'Add destination' }));
    expect(screen.getByLabelText('Link to organization')).toHaveValue('acme');
    await user.type(screen.getByLabelText('Nickname *'), PENDING.label);
    await user.type(screen.getByLabelText('AWS Account ID *'), PENDING.account_id);
    await user.click(screen.getByTestId('routing-register-download'));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(click.mock.instances[0]).toHaveAttribute('download', 'adp-bedrock.zip');
    expect(open).not.toHaveBeenCalled();
    expect(getDestinationSetup).toHaveBeenCalledWith(PENDING.id);
    expect(screen.getByLabelText('Link to organization')).toBeDisabled();
    await user.click(screen.getByTestId('routing-register-verify'));
    expect(await screen.findByTestId('routing-register-verify-failed')).toBeInTheDocument();
    expect(mockSetMapping).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Retry Verify' }));
    expect(await screen.findByLabelText('Organization')).toHaveValue('acme');
    expect(screen.getByLabelText('Team')).toHaveValue(APP_DEV.id);
    expect(screen.getByLabelText('Destination')).toHaveValue(PENDING.id);
    expect(mockSetMapping).not.toHaveBeenCalled();
    await user.click(screen.getByTestId('routing-rule-save'));
    expect(mockSetMapping).toHaveBeenCalledWith(expect.objectContaining({ scope_type: 'team', org: 'acme', team: APP_DEV.id }), PENDING.id);
    click.mockRestore();
  });

  it('resumes a saved destination after returning to the page without registering another one', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByTestId(`routing-resume-${PENDING.id}`));
    expect(screen.getByLabelText('AWS Account ID *')).toHaveValue(PENDING.account_id);
    expect(screen.getByLabelText('AWS Account ID *')).toBeDisabled();
    expect(screen.getByLabelText('Link to organization')).toHaveValue('acme');
    expect(screen.getByText(/Expected role ARN/)).toHaveTextContent(PENDING.account_id);
    await user.click(screen.getByTestId('routing-register-verify'));
    expect(mockVerifyDestination).toHaveBeenCalledWith(PENDING.id);
    expect(mockRegisterDestination).not.toHaveBeenCalled();
    expect(mockSetMapping).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('retries setup retrieval with the same saved destination and opens a fresh console link', async () => {
    const user = userEvent.setup();
    mockRegisterDestination.mockResolvedValue({ destination: PENDING, launch_url: 'https://old.example' });
    vi.mocked(getDestinationSetup).mockRejectedValueOnce({ detail: 'Setup details could not be read. Retry.' });
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    renderPanel();
    await user.click(await screen.findByTestId('routing-register-open'));
    await user.type(screen.getByLabelText('Nickname *'), 'new-dest');
    await user.type(screen.getByLabelText('AWS Account ID *'), '123456789012');
    await user.selectOptions(screen.getByLabelText('Link to organization'), 'acme');
    await user.click(screen.getByTestId('routing-register-launch'));
    expect(await screen.findByTestId('routing-register-error')).toHaveTextContent('Setup details could not be read. Retry.');
    expect(open).not.toHaveBeenCalled();
    await user.click(screen.getByTestId('routing-register-launch'));
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://console.aws.amazon.com/cloudformation/quickcreate?fresh=1', '_blank', 'noopener,noreferrer'));
    expect(mockRegisterDestination).toHaveBeenCalledOnce();
    expect(getDestinationSetup).toHaveBeenCalledTimes(2);
  });

  it('returns from destination setup to the unchanged organization rule', async () => {
    const user = userEvent.setup();
    renderPanel();
    await openAddRule(user, 'org');
    await user.selectOptions(screen.getByLabelText('Organization'), 'globex');
    await user.click(screen.getByRole('button', { name: 'Add destination' }));
    expect(screen.getByLabelText('Link to organization')).toHaveValue('globex');
    await user.click(screen.getByRole('button', { name: 'Close modal' }));
    expect(await screen.findByLabelText('Organization')).toHaveValue('globex');
    expect(screen.getByTestId('routing-scope-org')).toBeChecked();
    expect(mockRegisterDestination).not.toHaveBeenCalled();
  });

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


describe('existing connection links in the routing panel', () => {
  it('refreshes destinations after linking an existing connection', async () => {
    vi.mocked(listExistingAwsConnections).mockResolvedValue([{
      credential_id: 'existing', label: 'Team AWS', account_id: '123456789012', org_id: 'globex', org_name: 'Globex',
      owner_scope: 'user', owner_name: 'Account owner', status: 'verified', selectable: true, reason: null,
    }]);
    vi.mocked(linkAwsConnection).mockResolvedValue({ destination: { ...ACME_PROD, connection_id: 'existing' } });
    const user = userEvent.setup();
    renderPanel();
    await screen.findByTestId('routing-destination-dest-acme');
    mockListDestinations.mockClear();
    await user.click(screen.getByRole('button', { name: 'Use existing AWS connection' }));
    await screen.findByRole('option', { name: /Team AWS/ });
    await user.selectOptions(screen.getByLabelText('AWS connection'), 'existing');
    await user.selectOptions(screen.getByLabelText('Link to organization'), 'acme');
    await user.click(screen.getByRole('button', { name: 'Verify & link' }));
    await waitFor(() => expect(mockListDestinations).toHaveBeenCalled());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mockRegisterDestination).not.toHaveBeenCalled();
  });

  it('disables unlink for links used by a rule', async () => {
    mockListDestinations.mockResolvedValue([{ ...ACME_PROD, connection_id: 'existing', used_by: 1 }]);
    renderPanel();
    expect(await screen.findByRole('button', { name: 'Unlink' })).toBeDisabled();
  });

  it('preserves the modal on unlink conflict and refreshes after successful retry', async () => {
    mockListDestinations.mockResolvedValue([{ ...ACME_PROD, connection_id: 'existing', used_by: 0 }]);
    vi.mocked(unlinkAwsConnection)
      .mockRejectedValueOnce({ detail: 'Remove the routing rules before unlinking.' })
      .mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole('button', { name: 'Unlink' }));
    expect(screen.getByText(/original AWS connection and AWS role will remain unchanged/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Unlink connection' }));
    expect(await screen.findByText('Remove the routing rules before unlinking.')).toBeVisible();
    expect(screen.getByRole('dialog')).toBeVisible();
    mockListDestinations.mockClear();
    await user.click(screen.getByRole('button', { name: 'Unlink connection' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(unlinkAwsConnection).toHaveBeenCalledWith(ACME_PROD.id);
    expect(mockListDestinations).toHaveBeenCalled();
  });
});
