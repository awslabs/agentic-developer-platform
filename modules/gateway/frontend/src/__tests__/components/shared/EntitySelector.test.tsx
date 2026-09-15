/**
 * EntitySelector Component Tests
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Tests for the entity selector component that allows selecting entity types and IDs.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { EntitySelector } from '@/components/shared/EntitySelector';
import { EntityType } from '@/types';
import { PERSON_LIMIT_OPTION_VALUE } from '@/utils/entityLabels';

// Mock the admin service.
//
// Issue #4948: orgs, departments and teams come from the PLATFORM's tenancy tables,
// not from Cognito groups. The Cognito sources listed group names and scraped
// `custom:*` attributes off signed-in users, so a platform-natively created org was
// invisible to every governance form and a team was keyed by a name enforcement can
// never match. Users still come from Postgres via getOrgUsers, because that is the only
// source carrying the Cognito sub — the value user budgets are keyed by (#4511).
vi.mock('@/services/admin', () => ({
  getOrgUsers: vi.fn(),
  getOrganizations: vi.fn(),
  getDepartments: vi.fn(),
  getOrgTeams: vi.fn(),
}));

import { getOrgUsers, getOrganizations, getDepartments, getOrgTeams } from '@/services/admin';

const mockGetDepartments = getDepartments as ReturnType<typeof vi.fn>;
const mockGetTeams = getOrgTeams as ReturnType<typeof vi.fn>;
const mockGetUsers = getOrgUsers as ReturnType<typeof vi.fn>;
const mockGetOrganizations = getOrganizations as ReturnType<typeof vi.fn>;

const defaultProps = {
  orgId: 'org-001',
  entityType: EntityType.TEAM,
  entityId: '',
  onEntityTypeChange: vi.fn(),
  onEntityIdChange: vi.fn(),
  disabled: false,
};

// Typed from the component's own props rather than `defaultProps`, so opt-in props
// the defaults do not set (e.g. `allowCloudAgentScope`, #4536) are passable.
const renderComponent = (props: Partial<Parameters<typeof EntitySelector>[0]> = {}) => {
  return render(<EntitySelector {...defaultProps} {...props} />);
};

describe('EntitySelector', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // `id` and `name` are deliberately DIFFERENT strings throughout: a test whose
    // fixture named them the same could not tell an id-keyed option from a name-keyed
    // one, which is the exact confusion that shipped this defect.
    mockGetDepartments.mockResolvedValue({
      items: [
        { id: 'dept-001', orgId: 'org-001', name: 'Platform Engineering' },
        { id: 'dept-002', orgId: 'org-001', name: 'Data Science' },
      ],
      total: 2,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
    mockGetTeams.mockResolvedValue({
      items: [
        { id: 'team-001', departmentId: 'dept-001', name: 'Backend' },
        { id: 'team-002', departmentId: 'dept-001', name: 'Frontend' },
      ],
      total: 2,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
    mockGetOrganizations.mockResolvedValue({
      items: [
        { id: 'org-001', name: 'Acme Corp' },
        { id: 'sophos-it', name: 'Sophos IT' },
      ],
      total: 2,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
    mockGetUsers.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
  });

  describe('Entity Type Selection', () => {
    it('renders entity type selector with label', () => {
      renderComponent();
      expect(screen.getByText('Entity Type')).toBeInTheDocument();
    });

    it('calls onEntityTypeChange when entity type is changed', async () => {
      const user = userEvent.setup();
      const onEntityTypeChange = vi.fn();
      renderComponent({ onEntityTypeChange });

      const selects = screen.getAllByRole('combobox');
      const entityTypeSelect = selects[0];
      await user.selectOptions(entityTypeSelect, EntityType.DEPARTMENT);

      expect(onEntityTypeChange).toHaveBeenCalledWith(EntityType.DEPARTMENT);
    });
  });

  describe('Entity ID Selection - User', () => {
    it('shows manual input for user type', async () => {
      renderComponent({ entityType: EntityType.USER });

      await waitFor(() => {
        // User type shows text input
        const inputs = screen.getAllByRole('textbox');
        expect(inputs.length).toBeGreaterThan(0);
      });
    });

    // Issue #4511: the picker must supply the Cognito sub. It previously supplied
    // the Cognito Username (`GitHub_<github_id>`), which the budget engine never
    // matches, so a cap created for a real member silently never enforced.
    describe('#4511 — option values are Cognito subs', () => {
      const members = [
        {
          id: 'user-operator',
          email: 'operator@test.com',
          name: 'Operator',
          cognitoSub: '8a41f2c0-1b7d-4e5a-9c33-000000000001',
          role: 'org_admin',
        },
        {
          id: 'user-invited',
          email: 'invited@test.com',
          name: 'Invited Person',
          cognitoSub: null,
          role: 'member',
        },
      ];

      beforeEach(() => {
        mockGetUsers.mockResolvedValue({
          items: members,
          total: members.length,
          page: 1,
          pageSize: 100,
          hasMore: false,
        });
      });

      it('reads members from the org users endpoint, not the Cognito one', async () => {
        renderComponent({ entityType: EntityType.USER });

        await waitFor(() => {
          expect(mockGetUsers).toHaveBeenCalledWith('org-001', { pageSize: 100, page: 1 });
        });
      });

      it('uses the Cognito sub as the option value', async () => {
        renderComponent({ entityType: EntityType.USER });

        const option = await waitFor(() =>
          screen.getByRole('option', { name: /Operator/ })
        );
        expect(option).toHaveValue('8a41f2c0-1b7d-4e5a-9c33-000000000001');
      });

      it('never emits a GitHub-style username as the value', async () => {
        renderComponent({ entityType: EntityType.USER });

        const option = await waitFor(() =>
          screen.getByRole('option', { name: /Operator/ })
        );
        expect(option).not.toHaveValue(expect.stringContaining('GitHub_'));
      });

      it('renders members with no Cognito sub as disabled rather than hiding them', async () => {
        renderComponent({ entityType: EntityType.USER });

        const option = await waitFor(() =>
          screen.getByRole('option', { name: /Invited Person/ })
        );
        // Shown, so the operator can see why they cannot pick this person, but
        // unselectable, because a budget for them could never be enforced.
        expect(option).toBeDisabled();
      });

      it('selecting a member reports the sub to the parent form', async () => {
        const user = userEvent.setup();
        const onEntityIdChange = vi.fn();
        renderComponent({ entityType: EntityType.USER, onEntityIdChange });

        await waitFor(() => expect(screen.getByRole('option', { name: /Operator/ })).toBeInTheDocument());

        const selects = screen.getAllByRole('combobox');
        await user.selectOptions(selects[1], '8a41f2c0-1b7d-4e5a-9c33-000000000001');

        expect(onEntityIdChange).toHaveBeenCalledWith('8a41f2c0-1b7d-4e5a-9c33-000000000001');
      });
    });
  });

  // Issue #4536: the cloud-agent ledger is keyed by canonical `users.id`, not by
  // Cognito sub. Submitting the sub here would be #4511 one ledger over — a cap
  // that exists and matches nothing the usage tracker ever writes.
  describe('Entity ID Selection - Cloud agents (#4536)', () => {
    const members = [
      {
        id: 'user-operator',
        email: 'operator@test.com',
        name: 'Operator',
        cognitoSub: '8a41f2c0-1b7d-4e5a-9c33-000000000001',
        role: 'org_admin',
      },
      {
        id: 'user-invited',
        email: 'invited@test.com',
        name: 'Invited Person',
        cognitoSub: null,
        role: 'member',
      },
    ];

    beforeEach(() => {
      mockGetUsers.mockResolvedValue({
        items: members,
        total: members.length,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });
    });

    it('offers the cloud-agent scope only when the caller opts in', async () => {
      // Shared with the rate-limit form, which has no `root_user` enforcement —
      // offering it there would configure a limit that silently does nothing.
      renderComponent({ entityType: EntityType.TEAM });
      expect(
        screen.queryByRole('option', { name: 'User — cloud agents (this GitHub org)' })
      ).not.toBeInTheDocument();
    });

    it('keeps the plain "User" wording where the cloud-agent kind is not on offer', async () => {
      // "User — direct use" on the rate-limit form would imply a cloud-agents
      // counterpart exists there; nothing enforces one.
      renderComponent({ entityType: EntityType.USER });
      expect(screen.getByRole('option', { name: 'User' })).toBeInTheDocument();
      expect(screen.queryByRole('option', { name: 'User — direct use' })).not.toBeInTheDocument();
      expect(screen.queryByText(/traffic this person sends while signed in/i)).not.toBeInTheDocument();
    });

    it('offers both person-scoped kinds in plain language when opted in', async () => {
      renderComponent({ entityType: EntityType.TEAM, allowCloudAgentScope: true });

      expect(screen.getByRole('option', { name: 'User — direct use' })).toBeInTheDocument();
      // Issue #4687 added the scope qualifier: this option caps only the spend that
      // bills to the current workspace, and the unqualified wording read as global.
      expect(
        screen.getByRole('option', { name: 'User — cloud agents (this GitHub org)' })
      ).toBeInTheDocument();
    });

    it('never renders the internal entity-type name', async () => {
      renderComponent({ entityType: EntityType.ROOT_USER, allowCloudAgentScope: true });

      await waitFor(() => expect(mockGetUsers).toHaveBeenCalled());
      expect(document.body.textContent).not.toContain('root_user');
    });

    it('uses the SAME person picker as direct use', async () => {
      renderComponent({ entityType: EntityType.ROOT_USER, allowCloudAgentScope: true });

      await waitFor(() => {
        expect(mockGetUsers).toHaveBeenCalledWith('org-001', { pageSize: 100, page: 1 });
      });
    });

    it('uses the canonical user id as the option value, not the sub', async () => {
      renderComponent({ entityType: EntityType.ROOT_USER, allowCloudAgentScope: true });

      const option = await waitFor(() => screen.getByRole('option', { name: /Operator/ }));
      expect(option).toHaveValue('user-operator');
      expect(option).not.toHaveValue('8a41f2c0-1b7d-4e5a-9c33-000000000001');
    });

    it('leaves members with no Cognito sub selectable', async () => {
      // The one asymmetry with direct use: agent spend is attributed from the run's
      // lineage, not a signed-in session, so a never-signed-in member's canonical id
      // is a real enforceable key.
      renderComponent({ entityType: EntityType.ROOT_USER, allowCloudAgentScope: true });

      const option = await waitFor(() => screen.getByRole('option', { name: 'Invited Person (invited@test.com)' }));
      expect(option).toBeEnabled();
      expect(option).toHaveValue('user-invited');
    });

    it('selecting a member reports the canonical id to the parent form', async () => {
      const user = userEvent.setup();
      const onEntityIdChange = vi.fn();
      renderComponent({ entityType: EntityType.ROOT_USER, allowCloudAgentScope: true, onEntityIdChange });

      await waitFor(() => expect(screen.getByRole('option', { name: /Operator/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[1], 'user-operator');

      expect(onEntityIdChange).toHaveBeenCalledWith('user-operator');
    });

    it('explains which spend bucket each person-scoped kind governs', async () => {
      // The labels alone do not say which of a person's dollars land where, and
      // capping the wrong bucket while believing the other is bounded is the
      // failure mode the help text exists to prevent.
      const { unmount } = renderComponent({ entityType: EntityType.ROOT_USER, allowCloudAgentScope: true });
      expect(screen.getByText(/agent runs this person triggers/i)).toBeInTheDocument();
      unmount();

      renderComponent({ entityType: EntityType.USER, allowCloudAgentScope: true });
      expect(screen.getByText(/traffic this person sends while signed in/i)).toBeInTheDocument();
    });
  });

  // Issue #4687: the cross-workspace person limit. Not an entity type — it routes to
  // `PUT /budget/person-cap/{anchor}` instead of the budget-config create — so what this
  // component owes it is the person picker and nothing else. The anchor is resolved by
  // the consuming form from the picked member's server-held GitHub identity, so the
  // option value here stays the canonical `users.id` the picker already emits.
  describe('Entity ID Selection - Person limit (#4687)', () => {
    const members = [
      {
        id: 'user-operator',
        email: 'operator@test.com',
        name: 'Operator',
        cognitoSub: '8a41f2c0-1b7d-4e5a-9c33-000000000001',
        role: 'org_admin',
      },
    ];

    beforeEach(() => {
      mockGetUsers.mockResolvedValue({
        items: members,
        total: members.length,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });
    });

    it('does not offer the person limit unless the caller opts in', async () => {
      // Opt-in rather than opt-out because only the caller knows whether the signed-in
      // admin is a platform admin (#4620 §4.2), and a surface that forgets to pass the
      // flag should lose a feature, not hand out authority it never checked.
      renderComponent({ entityType: EntityType.TEAM, allowCloudAgentScope: true });
      expect(
        screen.queryByRole('option', { name: 'Person limit — all GitHub orgs' })
      ).not.toBeInTheDocument();
    });

    it('offers the person limit when the caller opts in', async () => {
      renderComponent({
        entityType: EntityType.TEAM,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      expect(
        screen.getByRole('option', { name: 'Person limit — all GitHub orgs' })
      ).toBeInTheDocument();
    });

    it('keeps the workspace-scoped cap on offer alongside it', async () => {
      // Both, always: the point of the pair is that an admin can see the choice they
      // are making. Replacing one with the other would trade this bug for its mirror.
      renderComponent({
        entityType: EntityType.TEAM,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      expect(
        screen.getByRole('option', { name: 'User — cloud agents (this GitHub org)' })
      ).toBeInTheDocument();
      expect(
        screen.getByRole('option', { name: 'Person limit — all GitHub orgs' })
      ).toBeInTheDocument();
    });

    it('reuses the same person picker as the workspace-scoped cap', async () => {
      renderComponent({
        entityType: PERSON_LIMIT_OPTION_VALUE,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      await waitFor(() => {
        expect(mockGetUsers).toHaveBeenCalledWith('org-001', { pageSize: 100, page: 1 });
      });
    });

    it('emits the canonical user id, which the form resolves to an anchor', async () => {
      // NOT the anchor itself: `getOrgUsers` does not carry the GitHub numeric id, so
      // anything anchor-shaped built here would be inferred client-side — a cap stored
      // under a key enforcement never reads (#4511).
      const user = userEvent.setup();
      const onEntityIdChange = vi.fn();
      renderComponent({
        entityType: PERSON_LIMIT_OPTION_VALUE,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
        onEntityIdChange,
      });

      await waitFor(() => expect(screen.getByRole('option', { name: /Operator/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[1], 'user-operator');

      expect(onEntityIdChange).toHaveBeenCalledWith('user-operator');
    });

    it('explains that the limit spans every workspace', async () => {
      renderComponent({
        entityType: PERSON_LIMIT_OPTION_VALUE,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      expect(screen.getByText(/across every GitHub org/i)).toBeInTheDocument();
    });

    it('offers no manual entry when the member list is empty', async () => {
      // Every other entity type falls back to a text box, which is right for a team or
      // department id. Here it would invite a typed anchor — the #4511 class exactly —
      // so the fallback is an explanation instead of an input.
      mockGetUsers.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 100, hasMore: false });

      renderComponent({
        entityType: PERSON_LIMIT_OPTION_VALUE,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      await waitFor(() => expect(screen.getByTestId('person-limit-no-picker')).toBeInTheDocument());
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    });

    it('offers no manual entry when the member list fails to load', async () => {
      mockGetUsers.mockRejectedValue(new Error('Fetch failed'));

      renderComponent({
        entityType: PERSON_LIMIT_OPTION_VALUE,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      await waitFor(() => expect(screen.getByTestId('person-limit-no-picker')).toBeInTheDocument());
      expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    });

    it('never renders the sentinel option value', async () => {
      // Same rule as `root_user` (#4536): internal identifiers are not user-facing.
      renderComponent({
        entityType: PERSON_LIMIT_OPTION_VALUE,
        allowCloudAgentScope: true,
        allowPersonLimitScope: true,
      });

      await waitFor(() => expect(mockGetUsers).toHaveBeenCalled());
      expect(document.body.textContent).not.toContain(PERSON_LIMIT_OPTION_VALUE);
    });
  });

  describe('Entity ID Selection - Team', () => {
    it('fetches teams from the org-wide tenancy route', async () => {
      // The ORG-WIDE route (PR #4917), not the department-scoped one: a budget may be
      // set on any team in the org, and a department-scoped list would silently hide
      // every team outside whichever department the admin was looking at.
      renderComponent({ entityType: EntityType.TEAM });

      await waitFor(() => {
        expect(mockGetTeams).toHaveBeenCalledWith('org-001', { pageSize: 100 });
      });
    });

    // Issue #4948: the value must be `teams.id`. The claim the team rung is matched
    // against (`custom:team_id`) is a projection of `users.team_id`, so a Cognito group
    // NAME — what this picker used to emit — stores a cap enforcement never matches.
    it('uses teams.id as the option value, never the team name', async () => {
      renderComponent({ entityType: EntityType.TEAM });

      const option = await waitFor(() => screen.getByRole('option', { name: /Backend/ }));
      expect(option).toHaveValue('team-001');
      expect(option).not.toHaveValue('Backend');
    });

    it('selecting a team reports its id to the parent form', async () => {
      const user = userEvent.setup();
      const onEntityIdChange = vi.fn();
      renderComponent({ entityType: EntityType.TEAM, onEntityIdChange });

      await waitFor(() => expect(screen.getByRole('option', { name: /Backend/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[selects.length - 1], 'team-001');

      expect(onEntityIdChange).toHaveBeenCalledWith('team-001');
    });

    it('shows the name alongside the id so the id is verifiable', async () => {
      renderComponent({ entityType: EntityType.TEAM });

      // Both, because the id is the thing that must be right and a name-only label
      // gives an operator no way to check which id they are about to store.
      const option = await waitFor(() => screen.getByRole('option', { name: 'Backend (team-001)' }));
      expect(option).toBeInTheDocument();
    });
  });

  describe('Entity ID Selection - Department', () => {
    it('fetches departments from the tenancy route', async () => {
      renderComponent({ entityType: EntityType.DEPARTMENT });

      await waitFor(() => {
        expect(mockGetDepartments).toHaveBeenCalledWith('org-001', { pageSize: 100 });
      });
    });

    // Issue #4948: the previous source scraped distinct `custom:department_id` values
    // off an org's signed-in Cognito users, so a department nobody had logged in from
    // was absent — and one nobody had joined could never be governed at all.
    it('uses departments.id as the option value, never the department name', async () => {
      renderComponent({ entityType: EntityType.DEPARTMENT });

      const option = await waitFor(() => screen.getByRole('option', { name: /Platform Engineering/ }));
      expect(option).toHaveValue('dept-001');
      expect(option).not.toHaveValue('Platform Engineering');
    });
  });

  // Issue #4948: the reported defect. The ORGANIZATION rung was not a list at all — a
  // single hardcoded option naming the caller's own org — so an org created through the
  // #4841 tenancy panels (`sophos-it` in the live repro) could not be given a budget or
  // a rate limit from any form.
  describe('Organization scope picker (#4948)', () => {
    it('offers no org picker to a consumer that cannot honour the write redirect', async () => {
      // Withheld deliberately: enforcement matches the partition AND the entity id, so
      // a form that keeps posting to the caller's own org while showing another org's
      // teams would author configs that match nothing. A consumer that forgets
      // `onScopeOrgChange` loses the affordance instead — the safe direction to fail.
      renderComponent({ entityType: EntityType.TEAM });

      await waitFor(() => expect(mockGetTeams).toHaveBeenCalled());
      expect(mockGetOrganizations).not.toHaveBeenCalled();
      expect(screen.queryByLabelText(/GitHub org/i)).not.toBeInTheDocument();
    });

    it('lists organizations from the platform tenancy route when opted in', async () => {
      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange: vi.fn() });

      await waitFor(() => {
        expect(mockGetOrganizations).toHaveBeenCalledWith({ pageSize: 100 });
      });
    });

    it('offers a platform-native org the Cognito source could never show', async () => {
      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange: vi.fn() });

      const option = await waitFor(() => screen.getByRole('option', { name: /Sophos IT/ }));
      expect(option).toHaveValue('sophos-it');
    });

    it('is not offered for person-scoped kinds', async () => {
      // Their partition is already correct and their keys are owned by #4511/#4536/#4687;
      // widening their org scope is out of this issue's scope.
      renderComponent({ entityType: EntityType.USER, onScopeOrgChange: vi.fn() });

      await waitFor(() => expect(mockGetUsers).toHaveBeenCalled());
      expect(mockGetOrganizations).not.toHaveBeenCalled();
    });

    it('reports the picked org to the parent so the WRITE follows it', async () => {
      // The anti-trap assertion. Without this callback firing, the form posts to the
      // caller's own `/organizations/{orgId}/budgets` and the row lands in a partition
      // enforcement never reads for the picked org's members — a green UI with configs
      // that never match, which is worse than the gap this issue reports.
      const user = userEvent.setup();
      const onScopeOrgChange = vi.fn();
      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange });

      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[1], 'sophos-it');

      expect(onScopeOrgChange).toHaveBeenCalledWith('sophos-it');
    });

    it('re-lists teams for the newly picked org', async () => {
      const user = userEvent.setup();
      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange: vi.fn() });

      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[1], 'sophos-it');

      await waitFor(() => {
        expect(mockGetTeams).toHaveBeenCalledWith('sophos-it', { pageSize: 100 });
      });
    });

    it('clears the picked entity when the org changes', async () => {
      // A team id from the previous org is exactly the cross-tenant config the cascade
      // exists to prevent, and it would look plausible on screen.
      const user = userEvent.setup();
      const onEntityIdChange = vi.fn();
      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange: vi.fn(), onEntityIdChange });

      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[1], 'sophos-it');

      expect(onEntityIdChange).toHaveBeenCalledWith('');
    });

    it('makes the org rung the picked org, not the caller\'s own', async () => {
      // The org budget's entity id and its write partition must be the same org, or the
      // row is unmatchable however real the entity is.
      const user = userEvent.setup();
      renderComponent({ entityType: EntityType.ORGANIZATION, onScopeOrgChange: vi.fn() });

      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());

      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[1], 'sophos-it');

      await waitFor(() => {
        const entitySelect = screen.getAllByRole('combobox')[2];
        expect(entitySelect).toContainHTML('sophos-it');
      });
    });

    it('discloses a truncated org list rather than letting absence read as non-existence', async () => {
      // The #4936 M4 rule. An org missing from page 1 reads as "no such org" — which is
      // precisely the misreading this whole issue is about.
      mockGetOrganizations.mockResolvedValue({
        items: [{ id: 'org-001', name: 'Acme Corp' }],
        total: 500,
        page: 1,
        pageSize: 100,
        hasMore: true,
      });

      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange: vi.fn() });

      await waitFor(() => expect(screen.getByTestId('orgs-truncated-warning')).toBeInTheDocument());
    });

    it('keeps the entity picker usable when the org list fails to load', async () => {
      // A failed org list is a lost affordance, not a broken form: the caller's own org
      // still works, which is what every single-org admin needs.
      mockGetOrganizations.mockRejectedValue(new Error('boom'));

      renderComponent({ entityType: EntityType.TEAM, onScopeOrgChange: vi.fn() });

      await waitFor(() => {
        expect(mockGetTeams).toHaveBeenCalledWith('org-001', { pageSize: 100 });
      });
      expect(screen.queryByTestId('orgs-truncated-warning')).not.toBeInTheDocument();
    });
  });

  describe('Loading State', () => {
    it('shows loading state while fetching entities', () => {
      // Make the request hang
      mockGetDepartments.mockImplementation(() => new Promise(() => {}));

      renderComponent({ entityType: EntityType.DEPARTMENT });

      expect(screen.getByText(/loading entities/i)).toBeInTheDocument();
    });
  });

  describe('Error Handling', () => {
    it('falls back to manual input on fetch error', async () => {
      mockGetDepartments.mockRejectedValue(new Error('Fetch failed'));

      renderComponent({ entityType: EntityType.DEPARTMENT });

      await waitFor(() => {
        // Should show text input after error
        const inputs = screen.getAllByRole('textbox');
        expect(inputs.length).toBeGreaterThan(0);
      });
    });
  });

  describe('Empty Results', () => {
    it('falls back to manual input when no entities found', async () => {
      mockGetDepartments.mockResolvedValue({
        items: [],
        total: 0,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });

      renderComponent({ entityType: EntityType.DEPARTMENT });

      await waitFor(() => {
        // Should show text input when no entities found
        const inputs = screen.getAllByRole('textbox');
        expect(inputs.length).toBeGreaterThan(0);
      });
    });
  });
});
