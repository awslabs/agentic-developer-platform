/**
 * Tests for the Organizations admin panel — Issue #4841 (#4839 · T2a).
 *
 * Covers the Validation section of the issue: creating an org with no GitHub involvement
 * (asserting the request targets the CANONICAL route and carries the caller-supplied
 * `id`), department CRUD, team CRUD, a non-admin seeing no panel, and a 403 from the
 * server surfacing as a readable message rather than a blank panel.
 *
 * Extended by Issue #4847 (T2b) with the Members tab: assignment writes membership ROWS
 * through T1's routes and never `users.team_id`, the picker is sourced from the org-wide
 * teams endpoint, every write refetches, and the server's structured second-primary
 * refusal is surfaced verbatim.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Organizations from '@/pages/admin/Organizations';

vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn(),
  createOrganizationCanonical: vi.fn(),
  getDepartments: vi.fn(),
  createDepartment: vi.fn(),
  updateDepartment: vi.fn(),
  deleteDepartment: vi.fn(),
  getTeams: vi.fn(),
  createTeam: vi.fn(),
  updateTeam: vi.fn(),
  deleteTeam: vi.fn(),
  // T1's membership surface (#4840), consumed by the Members tab. `updateUser` is
  // NOT mocked here on purpose: the page must not import it, because a `users.team_id`
  // write is precisely what this story forbids, and an unmocked import would fail
  // loudly rather than silently pass.
  getOrgUsers: vi.fn(),
  getOrgTeams: vi.fn(),
  getUserTeams: vi.fn(),
  addTeamMember: vi.fn(),
  replaceUserTeams: vi.fn(),
  removeTeamMember: vi.fn(),
  getMemberBudgets: vi.fn(),
  // PR #4936 review (M2): the role select reuses UserList's call, "Remove" is the
  // org-level deletion, and the add-member modal's PersonPicker reads the platform
  // roster. `removeUserRole` is NOT mocked on purpose: the page must not import it
  // — demote-to-member and remove-from-org are different acts, and wiring the
  // demotion behind a "Remove" button would be the conflation the review forbids.
  assignUserRole: vi.fn(),
  getAvailableRoles: vi.fn(),
  removeOrgUser: vi.fn(),
  listPlatformUsers: vi.fn(),
}));

const mockPermissions = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockPermissions(),
}));

import {
  getOrganizations,
  createOrganizationCanonical,
  getDepartments,
  createDepartment,
  updateDepartment,
  deleteDepartment,
  getTeams,
  createTeam,
  updateTeam,
  deleteTeam,
  getOrgUsers,
  getOrgTeams,
  getUserTeams,
  addTeamMember,
  replaceUserTeams,
  removeTeamMember,
  getMemberBudgets,
  assignUserRole,
  getAvailableRoles,
  removeOrgUser,
  listPlatformUsers,
} from '@/services/admin';

const mockGetOrgs = getOrganizations as ReturnType<typeof vi.fn>;
const mockCreateOrg = createOrganizationCanonical as ReturnType<typeof vi.fn>;
const mockGetDepts = getDepartments as ReturnType<typeof vi.fn>;
const mockCreateDept = createDepartment as ReturnType<typeof vi.fn>;
const mockUpdateDept = updateDepartment as ReturnType<typeof vi.fn>;
const mockDeleteDept = deleteDepartment as ReturnType<typeof vi.fn>;
const mockGetTeams = getTeams as ReturnType<typeof vi.fn>;
const mockCreateTeam = createTeam as ReturnType<typeof vi.fn>;
const mockUpdateTeam = updateTeam as ReturnType<typeof vi.fn>;
const mockDeleteTeam = deleteTeam as ReturnType<typeof vi.fn>;
const mockGetOrgUsers = getOrgUsers as ReturnType<typeof vi.fn>;
const mockGetOrgTeams = getOrgTeams as ReturnType<typeof vi.fn>;
const mockGetUserTeams = getUserTeams as ReturnType<typeof vi.fn>;
const mockAddTeamMember = addTeamMember as ReturnType<typeof vi.fn>;
const mockReplaceUserTeams = replaceUserTeams as ReturnType<typeof vi.fn>;
const mockRemoveTeamMember = removeTeamMember as ReturnType<typeof vi.fn>;
const mockGetMemberBudgets = getMemberBudgets as ReturnType<typeof vi.fn>;
const mockAssignUserRole = assignUserRole as ReturnType<typeof vi.fn>;
const mockGetAvailableRoles = getAvailableRoles as ReturnType<typeof vi.fn>;
const mockRemoveOrgUser = removeOrgUser as ReturnType<typeof vi.fn>;
const mockListPlatformUsers = listPlatformUsers as ReturnType<typeof vi.fn>;

/** Platform admin by default — the role that can do everything on this panel. */
function permissions(overrides: Record<string, unknown> = {}) {
  return {
    canViewOrganizations: () => true,
    canUpdateOrganizations: () => true,
    canCreateOrganizations: () => true,
    isPlatformAdmin: () => true,
    ...overrides,
  };
}

const ORGS = {
  items: [
    {
      id: 'sophos',
      name: 'Sophos',
      awsAccounts: ['123456789012'],
      roleMappings: {},
      settings: {},
      githubInstallationIds: ['12345678'],
      createdAt: '2026-09-02T00:00:00Z',
    },
    {
      id: 'design-studio',
      name: 'Design Studio',
      awsAccounts: [],
      roleMappings: {},
      settings: {},
      githubInstallationIds: [],
      createdAt: '2026-09-08T00:00:00Z',
    },
  ],
  total: 2,
  page: 1,
  pageSize: 100,
  hasMore: false,
};

/** What the canonical route auto-creates: `{org_id}-dept-default`. */
const DEPTS = {
  items: [
    {
      id: 'sophos-dept-default',
      orgId: 'sophos',
      name: 'Default',
      description: 'Default department',
      createdAt: '2026-09-02T00:00:00Z',
    },
    {
      id: 'sophos-dept-eng',
      orgId: 'sophos',
      name: 'Engineering',
      createdAt: '2026-09-03T00:00:00Z',
    },
  ],
  total: 2,
  page: 1,
  pageSize: 50,
  hasMore: false,
};

const TEAMS = {
  items: [
    {
      id: 'sophos-team-default',
      departmentId: 'sophos-dept-default',
      name: 'Default',
      description: 'Default team',
      createdAt: '2026-09-02T00:00:00Z',
    },
  ],
  total: 1,
  page: 1,
  pageSize: 50,
  hasMore: false,
};

/** The Members tab's fixtures — Issue #4847. */
const ORG_USERS = {
  items: [
    {
      id: 'user-jane',
      email: 'jane@sophos.test',
      name: 'Jane Doe',
      cognitoSub: 'sub-jane',
      role: 'member',
      githubUsername: 'jdoe-gh',
    },
  ],
  total: 1,
  page: 1,
  pageSize: 50,
  hasMore: false,
};

/**
 * The ORG-WIDE team list, spanning two departments.
 *
 * Deliberately different from `TEAMS` above (which is department-scoped and holds only
 * `sophos-team-default`): the picker must be populated from this one, so a test that
 * finds `web-team` in it proves the page called the org-wide endpoint.
 */
const ORG_TEAMS = {
  items: [
    {
      id: 'sophos-team-ml',
      departmentId: 'sophos-dept-eng',
      name: 'ml-team',
      createdAt: '2026-09-03T00:00:00Z',
    },
    {
      id: 'sophos-team-web',
      departmentId: 'sophos-dept-design',
      name: 'web-team',
      createdAt: '2026-09-03T00:00:00Z',
    },
  ],
  total: 2,
  page: 1,
  pageSize: 100,
  hasMore: false,
};

function membershipRow(teamId: string, isPrimary = false) {
  return {
    id: `m-${teamId}`,
    userId: 'user-jane',
    teamId,
    orgId: 'sophos',
    role: 'member',
    isPrimary,
    source: 'admin',
    externalId: null,
    createdAt: '2026-09-03T00:00:00Z',
    updatedAt: null,
  };
}

function renderPanel() {
  return render(
    <MemoryRouter>
      <Organizations />
    </MemoryRouter>
  );
}

/**
 * A department's row in the Departments table.
 *
 * Queried by ROLE, not by text: each department name also appears as an <option> in the
 * team-selector, so `getByText('Engineering')` is ambiguous. Only the table renders the
 * name as a link (to the department dashboard), so the role pins it to the row.
 */
function departmentRow(name: string): HTMLElement {
  return screen.getByRole('link', { name }).closest('tr')!;
}

/** Open the Sophos org's structure section and wait for its departments to render. */
async function openSophos(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
  const row = screen.getByText('Sophos').closest('tr')!;
  await user.click(within(row).getByRole('button', { name: 'Open' }));
  await waitFor(() => expect(departmentRow('Engineering')).toBeInTheDocument());
}

/** Open Sophos, switch to the Members tab, and wait for the roster to render. */
async function openMembers(user: ReturnType<typeof userEvent.setup>) {
  await openSophos(user);
  await user.click(screen.getByRole('tab', { name: 'Members' }));
  await waitFor(() => expect(screen.getByText('Jane Doe')).toBeInTheDocument());
}

/** Open the per-member team dialog from the Members tab. */
async function openTeamsDialog(user: ReturnType<typeof userEvent.setup>) {
  const row = screen.getByText('Jane Doe').closest('tr')!;
  await user.click(within(row).getByRole('button', { name: 'Teams' }));
  await waitFor(() =>
    expect(screen.getByRole('heading', { name: /Teams for/ })).toBeInTheDocument()
  );
}

describe('Organizations admin panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPermissions.mockReturnValue(permissions());
    mockGetOrgs.mockResolvedValue({ ...ORGS });
    mockGetDepts.mockResolvedValue({ ...DEPTS });
    mockGetTeams.mockResolvedValue({ ...TEAMS });
    mockCreateOrg.mockResolvedValue({ id: 'acme-corp', name: 'Acme Corp' });
    mockCreateDept.mockResolvedValue({});
    mockUpdateDept.mockResolvedValue({});
    mockDeleteDept.mockResolvedValue(undefined);
    mockCreateTeam.mockResolvedValue({});
    mockUpdateTeam.mockResolvedValue({});
    mockDeleteTeam.mockResolvedValue(undefined);
    mockGetOrgUsers.mockResolvedValue({ ...ORG_USERS });
    mockGetOrgTeams.mockResolvedValue({ ...ORG_TEAMS });
    mockGetUserTeams.mockResolvedValue([membershipRow('sophos-team-ml', true)]);
    mockAddTeamMember.mockResolvedValue(membershipRow('sophos-team-web'));
    mockReplaceUserTeams.mockResolvedValue([
      membershipRow('sophos-team-ml'),
      membershipRow('sophos-team-web', true),
    ]);
    mockRemoveTeamMember.mockResolvedValue(undefined);
    mockGetMemberBudgets.mockResolvedValue({ items: [], total: 0, hasMore: false });
    mockAssignUserRole.mockResolvedValue({});
    mockGetAvailableRoles.mockResolvedValue(['member', 'dept_admin', 'org_admin']);
    mockRemoveOrgUser.mockResolvedValue(undefined);
    mockListPlatformUsers.mockResolvedValue({
      items: [
        {
          id: 'user-new',
          orgId: 'design-studio',
          email: 'new@studio.test',
          name: 'New Person',
          githubUsername: 'newbie',
        },
      ],
      total: 1,
      page: 1,
      pageSize: 50,
      hasMore: false,
    });
  });

  describe('organization list', () => {
    it('lists organizations with their immutable identifiers', async () => {
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      // The identifier is shown in full — it is the value that appears in routing scopes.
      expect(screen.getByText('sophos')).toBeInTheDocument();
      expect(screen.getByText('design-studio')).toBeInTheDocument();
    });

    it('shows a GitHub-free org as first-class rather than as missing data', async () => {
      renderPanel();

      await waitFor(() => expect(screen.getByText('Design Studio')).toBeInTheDocument());
      const row = screen.getByText('Design Studio').closest('tr')!;
      expect(within(row).getByText('none — platform-native')).toBeInTheDocument();
    });

    it('shows connection badges for an org that has them', async () => {
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      const row = screen.getByText('Sophos').closest('tr')!;
      expect(within(row).getByText(/GitHub: 1 installation/)).toBeInTheDocument();
      expect(within(row).getByText(/1 routing destination/)).toBeInTheDocument();
      expect(within(row).queryByText('none — platform-native')).not.toBeInTheDocument();
    });

    it('filters the list by name or identifier', async () => {
      const user = userEvent.setup();
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      await user.type(screen.getByLabelText('Search organizations'), 'design');

      expect(screen.queryByText('Sophos')).not.toBeInTheDocument();
      expect(screen.getByText('Design Studio')).toBeInTheDocument();
    });
  });

  describe('create organization', () => {
    it('targets the canonical route with the caller-supplied id and no GitHub input', async () => {
      const user = userEvent.setup();
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      await user.click(screen.getByRole('button', { name: '+ Create organization' }));

      await user.type(screen.getByLabelText(/Name/), 'Acme Corp');
      // The identifier is derived from the name and shown for confirmation (D4=A).
      expect(screen.getByLabelText(/Identifier/)).toHaveValue('acme-corp');

      // No GitHub affordance anywhere in the create flow — that is the point of T2a.
      expect(screen.getByText(/No GitHub needed/)).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Create organization' }));

      await waitFor(() => {
        // The canonical client function, NOT the deprecated `createOrganization`.
        expect(mockCreateOrg).toHaveBeenCalledWith({ id: 'acme-corp', name: 'Acme Corp' });
      });
    });

    it('lets the admin override the derived identifier and stops re-deriving it', async () => {
      const user = userEvent.setup();
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      await user.click(screen.getByRole('button', { name: '+ Create organization' }));

      await user.type(screen.getByLabelText(/Name/), 'Acme');
      await user.clear(screen.getByLabelText(/Identifier/));
      await user.type(screen.getByLabelText(/Identifier/), 'acme-emea');

      // A later keystroke in the name must not clobber the deliberate identifier.
      await user.type(screen.getByLabelText(/Name/), ' Corp');
      expect(screen.getByLabelText(/Identifier/)).toHaveValue('acme-emea');

      await user.click(screen.getByRole('button', { name: 'Create organization' }));
      await waitFor(() =>
        expect(mockCreateOrg).toHaveBeenCalledWith({ id: 'acme-emea', name: 'Acme Corp' })
      );
    });

    it('cannot submit without a derivable identifier', async () => {
      const user = userEvent.setup();
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      await user.click(screen.getByRole('button', { name: '+ Create organization' }));

      // A CJK-only name derives to '' — submitting would be a guaranteed 422.
      await user.type(screen.getByLabelText(/Name/), '株式会社');
      expect(screen.getByRole('button', { name: 'Create organization' })).toBeDisabled();
      expect(mockCreateOrg).not.toHaveBeenCalled();
    });

    it('presents the auto-created default department and team as normal state', async () => {
      const user = userEvent.setup();
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      await user.click(screen.getByRole('button', { name: '+ Create organization' }));
      await user.type(screen.getByLabelText(/Name/), 'Acme Corp');
      await user.click(screen.getByRole('button', { name: 'Create organization' }));

      await waitFor(() => {
        const notice = screen.getByRole('status');
        expect(notice).toHaveTextContent(/default department and team/);
        // Never framed as a problem.
        expect(notice.textContent).not.toMatch(/error|warning|failed|unexpected/i);
      });
    });

    it('surfaces a duplicate-identifier conflict as the server message', async () => {
      const user = userEvent.setup();
      // The identity router wraps every create failure as a 409 carrying the reason; that
      // message is the only thing that tells the admin their identifier is taken.
      mockCreateOrg.mockRejectedValue({
        error: 'conflict',
        message: 'Organization sophos already exists',
      });
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      await user.click(screen.getByRole('button', { name: '+ Create organization' }));
      await user.type(screen.getByLabelText(/Name/), 'Sophos');
      await user.click(screen.getByRole('button', { name: 'Create organization' }));

      await waitFor(() =>
        expect(screen.getByRole('alert')).toHaveTextContent('Organization sophos already exists')
      );
    });
  });

  describe('department CRUD', () => {
    it('creates a department in the selected org', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      await user.click(screen.getByRole('button', { name: 'Add Department' }));
      await user.type(screen.getByLabelText(/Name/), 'Security');
      await user.click(screen.getByRole('button', { name: 'Add department' }));

      await waitFor(() =>
        expect(mockCreateDept).toHaveBeenCalledWith('sophos', { name: 'Security' })
      );
    });

    it('renames a department', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      await user.click(within(departmentRow('Engineering')).getByRole('button', { name: 'Edit' }));

      const nameField = screen.getByLabelText(/Name/);
      await user.clear(nameField);
      await user.type(nameField, 'Platform Engineering');
      await user.click(screen.getByRole('button', { name: 'Save' }));

      await waitFor(() =>
        expect(mockUpdateDept).toHaveBeenCalledWith('sophos', 'sophos-dept-eng', {
          name: 'Platform Engineering',
        })
      );
    });

    it('requires a confirmation before deleting a department', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      const row = departmentRow('Engineering');
      await user.click(within(row).getByRole('button', { name: 'Delete' }));

      // Persistent confirmation, per the established admin-panel convention.
      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveTextContent(/cannot be undone/i);
      expect(mockDeleteDept).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole('button', { name: 'Delete' }));
      await waitFor(() =>
        expect(mockDeleteDept).toHaveBeenCalledWith('sophos', 'sophos-dept-eng')
      );
    });

    it('shows the auto-created default department as an ordinary row', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      // Rendered by the same table, with the same affordances, as any other department.
      expect(departmentRow('Default')).toBeInTheDocument();
      expect(within(departmentRow('Default')).getByRole('button', { name: 'Edit' })).toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
  });

  describe('team CRUD', () => {
    it('loads teams for the selected department', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      await waitFor(() =>
        expect(mockGetTeams).toHaveBeenCalledWith('sophos', 'sophos-dept-default')
      );
    });

    it('creates a team under the selected department', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      await user.click(await screen.findByRole('button', { name: 'Add Team' }));
      // By placeholder, not by label: `TeamManagement` passes `label` to `Input` without a
      // `name`, so `Input` derives no id and the <label for> is orphaned. That is a
      // pre-existing a11y gap in the shipped component, out of scope for #4841 (which
      // reuses it as-is) — noted rather than fixed here.
      await user.type(screen.getByPlaceholderText('Enter team name'), 'sre-team');
      await user.click(screen.getByRole('button', { name: 'Create' }));

      await waitFor(() =>
        expect(mockCreateTeam).toHaveBeenCalledWith('sophos', 'sophos-dept-default', {
          name: 'sre-team',
          description: undefined,
        })
      );
    });

    it('switches the team list when another department is selected', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      await user.selectOptions(screen.getByLabelText(/Teams in department/), 'sophos-dept-eng');

      await waitFor(() =>
        expect(mockGetTeams).toHaveBeenCalledWith('sophos', 'sophos-dept-eng')
      );
    });

    it('deletes a team', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      // The team's description cell is unique to the Teams table, so it pins the row.
      const row = (await screen.findByText('Default team')).closest('tr')!;
      await user.click(within(row).getByRole('button', { name: 'Delete' }));

      const dialog = screen.getByRole('dialog');
      await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

      await waitFor(() =>
        expect(mockDeleteTeam).toHaveBeenCalledWith('sophos', 'sophos-team-default')
      );
    });
  });

  describe('authorization affordances', () => {
    it('hides the whole panel from a caller without ORG_READ', () => {
      mockPermissions.mockReturnValue(
        permissions({ canViewOrganizations: () => false, isPlatformAdmin: () => false })
      );
      renderPanel();

      expect(screen.getByText(/do not have permission to view organizations/i)).toBeInTheDocument();
      // And it does not even ask — no dead-end request.
      expect(mockGetOrgs).not.toHaveBeenCalled();
    });

    it('hides create-organization from an org admin but still shows the list', async () => {
      // The canonical route is `require_admin` (platform-admin only), which deliberately
      // excludes org_admin — so the button must be gated on isPlatformAdmin(), not on the
      // ORG_CREATE permission. An org admin still manages their own structure.
      mockPermissions.mockReturnValue(
        permissions({ isPlatformAdmin: () => false, canCreateOrganizations: () => false })
      );
      renderPanel();

      await waitFor(() => expect(screen.getByText('Sophos')).toBeInTheDocument());
      expect(
        screen.queryByRole('button', { name: '+ Create organization' })
      ).not.toBeInTheDocument();
    });

    it('hides department and team write affordances without ORG_UPDATE', async () => {
      const user = userEvent.setup();
      mockPermissions.mockReturnValue(permissions({ canUpdateOrganizations: () => false }));
      renderPanel();
      await openSophos(user);

      expect(screen.queryByRole('button', { name: 'Add Department' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Add Team' })).not.toBeInTheDocument();
    });
  });

  describe('server errors', () => {
    it('renders a 403 on the org list as a readable message, not a blank panel', async () => {
      mockGetOrgs.mockRejectedValue({
        error: 'access_denied',
        message: 'Access denied: org:read required',
      });
      renderPanel();

      await waitFor(() =>
        expect(screen.getByRole('alert')).toHaveTextContent('Access denied: org:read required')
      );
      // The panel is still there, with its heading and its create affordance.
      expect(screen.getByRole('heading', { name: 'Organizations' })).toBeInTheDocument();
    });

    it('renders a 403 on a department write as a readable message', async () => {
      const user = userEvent.setup();
      mockCreateDept.mockRejectedValue({
        error: 'access_denied',
        message: 'Access denied: org:update required for sophos',
      });
      renderPanel();
      await openSophos(user);

      await user.click(screen.getByRole('button', { name: 'Add Department' }));
      await user.type(screen.getByLabelText(/Name/), 'Security');
      await user.click(screen.getByRole('button', { name: 'Add department' }));

      await waitFor(() =>
        expect(screen.getByRole('alert')).toHaveTextContent(
          'Access denied: org:update required for sophos'
        )
      );
    });

    it('falls back to readable copy when the error carries no message', async () => {
      mockGetOrgs.mockRejectedValue(new Error(''));
      renderPanel();

      await waitFor(() =>
        expect(screen.getByRole('alert')).toHaveTextContent('Failed to load organizations.')
      );
    });
  });

  // =========================================================================
  // Members panel & multi-team assignment — Issue #4847 (#4839 · T2b)
  // =========================================================================

  describe('members panel', () => {
    it('is not fetched until the tab is opened', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);

      // Opening an org loads its structure only. A roster nobody is looking at is a
      // request per member for nothing.
      expect(mockGetOrgUsers).not.toHaveBeenCalled();

      await user.click(screen.getByRole('tab', { name: 'Members' }));
      await waitFor(() => expect(mockGetOrgUsers).toHaveBeenCalledWith('sophos', expect.anything()));
    });

    it('lists members with their GitHub label and their team chips', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      expect(screen.getByText('jdoe-gh')).toBeInTheDocument();
      expect(screen.getByTestId('team-chip-sophos-team-ml')).toBeInTheDocument();
    });

    it('renders one chip per membership with exactly one primary for a two-team member', async () => {
      mockGetUserTeams.mockResolvedValue([
        membershipRow('sophos-team-ml', true),
        membershipRow('sophos-team-web'),
      ]);
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      const row = screen.getByText('Jane Doe').closest('tr')!;
      expect(within(row).getByTestId('team-chip-sophos-team-ml')).toBeInTheDocument();
      expect(within(row).getByTestId('team-chip-sophos-team-web')).toBeInTheDocument();
      expect(within(row).getAllByText('primary')).toHaveLength(1);
    });

    it('populates the team picker from the ORG-WIDE endpoint, not the department list', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      await openTeamsDialog(user);

      expect(mockGetOrgTeams).toHaveBeenCalledWith('sophos', expect.anything());
      // `web-team` exists only in the org-wide fixture, and in a DIFFERENT department
      // from the one selected on the Structure tab. Sourcing the picker from the
      // department-scoped list would hide it.
      const options = within(screen.getByLabelText('Add to team'))
        .getAllByRole('option')
        .map((o) => o.textContent);
      expect(options).toContain('web-team');
    });

    it('assigns a second team by writing a membership ROW, never users.team_id', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      await openTeamsDialog(user);

      await user.selectOptions(screen.getByLabelText('Add to team'), 'sophos-team-web');
      await user.click(screen.getByRole('button', { name: 'Add' }));

      // The story's central correctness assertion: a membership create against T1's
      // route, keyed by (org, team, user) — NOT an update of the user's single-valued
      // team pointer, which the server maintains as a cache of the primary.
      await waitFor(() =>
        expect(mockAddTeamMember).toHaveBeenCalledWith('sophos', 'sophos-team-web', {
          userId: 'user-jane',
        })
      );
      // No primary flag on an add: moving the primary is its own deliberate action.
      expect(mockAddTeamMember.mock.calls[0][2]).not.toHaveProperty('isPrimary', true);
    });

    it('refetches after an assignment rather than mutating its local copy', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      const readsBefore = mockGetUserTeams.mock.calls.length;
      await openTeamsDialog(user);

      await user.selectOptions(screen.getByLabelText('Add to team'), 'sophos-team-web');
      await user.click(screen.getByRole('button', { name: 'Add' }));

      // A write can move the primary server-side, so the panel re-reads the rows
      // instead of guessing what they became.
      await waitFor(() =>
        expect(mockGetUserTeams.mock.calls.length).toBeGreaterThan(readsBefore)
      );
    });

    it('sets the primary through the replace-set route with exactly one primary', async () => {
      mockGetUserTeams.mockResolvedValue([
        membershipRow('sophos-team-ml', true),
        membershipRow('sophos-team-web'),
      ]);
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      await openTeamsDialog(user);

      await user.click(
        within(screen.getByTestId('managed-team-sophos-team-web')).getByRole('button', {
          name: 'Make primary',
        })
      );

      // The FULL intended set, with the flag moved — the only shape in which "exactly
      // one primary" is expressible. `addTeamMember({isPrimary: true})` would be
      // refused with the 409 instead.
      await waitFor(() =>
        expect(mockReplaceUserTeams).toHaveBeenCalledWith('sophos', 'user-jane', [
          { teamId: 'sophos-team-ml', role: 'member', isPrimary: false },
          { teamId: 'sophos-team-web', role: 'member', isPrimary: true },
        ])
      );
    });

    it('surfaces the server second-primary refusal verbatim', async () => {
      mockGetUserTeams.mockResolvedValue([
        membershipRow('sophos-team-ml', true),
        membershipRow('sophos-team-web'),
      ]);
      // T1's stable contract: code `team_membership_second_primary`, HTTP 409, with a
      // human message the panel must not paraphrase.
      mockReplaceUserTeams.mockRejectedValue({
        error: 'team_membership_second_primary',
        message: 'User user-jane already has primary team sophos-team-ml in org sophos.',
        details: {
          user_id: 'user-jane',
          existing_primary_team_id: 'sophos-team-ml',
          requested_primary_team_id: 'sophos-team-web',
        },
      });
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      await openTeamsDialog(user);

      await user.click(
        within(screen.getByTestId('managed-team-sophos-team-web')).getByRole('button', {
          name: 'Make primary',
        })
      );

      await waitFor(() =>
        expect(screen.getByRole('alert')).toHaveTextContent(
          'User user-jane already has primary team sophos-team-ml in org sophos.'
        )
      );
      // Not swallowed and not replaced with a client-side guess at the wording.
      expect(screen.getByRole('alert')).not.toHaveTextContent('Failed to change the primary team.');
    });

    it('removes a membership and refetches so the chip disappears', async () => {
      mockGetUserTeams
        .mockResolvedValueOnce([
          membershipRow('sophos-team-ml', true),
          membershipRow('sophos-team-web'),
        ])
        .mockResolvedValue([membershipRow('sophos-team-ml', true)]);
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      await waitFor(() =>
        expect(screen.getByTestId('team-chip-sophos-team-web')).toBeInTheDocument()
      );
      await openTeamsDialog(user);

      await user.click(
        within(screen.getByTestId('managed-team-sophos-team-web')).getByRole('button', {
          name: 'Remove',
        })
      );

      await waitFor(() =>
        expect(mockRemoveTeamMember).toHaveBeenCalledWith('sophos', 'sophos-team-web', 'user-jane')
      );
      await waitFor(() =>
        expect(screen.queryByTestId('team-chip-sophos-team-web')).not.toBeInTheDocument()
      );
    });

    it('shows spend against the applicable limit for a platform admin', async () => {
      mockGetMemberBudgets.mockResolvedValue({
        items: [
          {
            userId: 'user-jane',
            spendUsd: '38.500000',
            limitUsd: '75.00',
            source: 'admin',
            // The server's third-person label for this surface (PR #4936 M1) —
            // never the ladder's "a limit set for YOU by…" prose.
            sourceLabel: 'individual limit',
            isCapped: true,
          },
        ],
        total: 1,
        hasMore: false,
      });
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      await waitFor(() => expect(screen.getByText('$38.5 / $75')).toBeInTheDocument());
      expect(screen.getByText('individual limit')).toBeInTheDocument();
    });

    it('skips the budget read for an org admin instead of firing a known 403', async () => {
      // The batch read is platform-admin-only — a narrower gate than this panel's,
      // because a person limit can disclose a ceiling from another tenant (#4620).
      mockPermissions.mockReturnValue(permissions({ isPlatformAdmin: () => false }));
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      expect(mockGetMemberBudgets).not.toHaveBeenCalled();
      // The panel itself still works fully — only the column is absent.
      expect(screen.queryByText('Spend / limit (month)')).not.toBeInTheDocument();
      expect(screen.getByText('Jane Doe')).toBeInTheDocument();
    });

    it('hides the assignment affordance without ORG_UPDATE', async () => {
      mockPermissions.mockReturnValue(permissions({ canUpdateOrganizations: () => false }));
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      expect(screen.queryByRole('button', { name: 'Teams' })).not.toBeInTheDocument();
    });

    it('shows no members panel at all to a caller who cannot read organizations', () => {
      mockPermissions.mockReturnValue(permissions({ canViewOrganizations: () => false }));
      renderPanel();

      expect(screen.queryByRole('tab', { name: 'Members' })).not.toBeInTheDocument();
      expect(mockGetOrgUsers).not.toHaveBeenCalled();
    });

    it('surfaces a cross-org refusal from the server rather than filtering it away', async () => {
      // Tenant isolation is the server's job; the panel's gating is affordance only. A
      // refusal must be readable, not silently rendered as an empty roster.
      mockGetOrgUsers.mockRejectedValue({
        error: 'access_denied',
        message: 'Access denied: org:read required for sophos',
      });
      const user = userEvent.setup();
      renderPanel();
      await openSophos(user);
      await user.click(screen.getByRole('tab', { name: 'Members' }));

      await waitFor(() =>
        expect(screen.getByRole('alert')).toHaveTextContent(
          'Access denied: org:read required for sophos'
        )
      );
    });
  });

  // =========================================================================
  // Members panel affordances — PR #4936 review (M2 role/remove/search/add,
  // M4 truncation)
  // =========================================================================

  describe('members panel affordances (#4936)', () => {
    it('changes a role through the SAME client call UserList uses', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      await user.selectOptions(screen.getByLabelText('Role for Jane Doe'), 'org_admin');

      // `assignUserRole` → PUT /admin/organizations/{org}/users/{user} — reuse, not a
      // fork. The server enforces the ceiling; the page only wires the affordance.
      await waitFor(() =>
        expect(mockAssignUserRole).toHaveBeenCalledWith({
          user_id: 'user-jane',
          role: 'org_admin',
          org_id: 'sophos',
        })
      );
      // And refetches rather than mutating its local copy.
      await waitFor(() =>
        expect(mockGetOrgUsers.mock.calls.length).toBeGreaterThan(1)
      );
    });

    it('offers the ceiling-filtered role list from GET /admin/users/roles', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      await waitFor(() => expect(mockGetAvailableRoles).toHaveBeenCalled());
      const options = within(screen.getByLabelText('Role for Jane Doe'))
        .getAllByRole('option')
        .map((o) => o.textContent);
      expect(options).toEqual(['Member', 'Dept Admin', 'Org Admin']);
    });

    it('removes a member from the ORG through the delete route, behind a confirmation', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      const row = screen.getByText('Jane Doe').closest('tr')!;
      await user.click(within(row).getByRole('button', { name: 'Remove' }));

      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveTextContent(/not a role change/i);
      expect(mockRemoveOrgUser).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole('button', { name: 'Remove member' }));

      // `removeOrgUser` → DELETE .../users/{user}: org-level removal. NOT
      // `removeUserRole`, which demotes and would leave the person in the org.
      await waitFor(() => expect(mockRemoveOrgUser).toHaveBeenCalledWith('sophos', 'user-jane'));
      // Refetched, so the roster reflects the deletion.
      await waitFor(() => expect(mockGetOrgUsers.mock.calls.length).toBeGreaterThan(1));
    });

    it('filters the loaded roster by name or GitHub username, client-side', async () => {
      mockGetOrgUsers.mockResolvedValue({
        ...ORG_USERS,
        items: [
          ...ORG_USERS.items,
          {
            id: 'user-sam',
            email: 'sam@sophos.test',
            name: 'Sam Field',
            cognitoSub: 'sub-sam',
            role: 'member',
            githubUsername: null,
          },
        ],
        total: 2,
      });
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);
      expect(screen.getByText('Sam Field')).toBeInTheDocument();

      // By GitHub username…
      await user.type(screen.getByLabelText('Search members'), 'jdoe');
      expect(screen.getByText('Jane Doe')).toBeInTheDocument();
      expect(screen.queryByText('Sam Field')).not.toBeInTheDocument();

      // …and by name.
      await user.clear(screen.getByLabelText('Search members'));
      await user.type(screen.getByLabelText('Search members'), 'sam field');
      expect(screen.getByText('Sam Field')).toBeInTheDocument();
      expect(screen.queryByText('Jane Doe')).not.toBeInTheDocument();
    });

    it('adds a member through the T1 membership call from the assign-member modal', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      await user.click(screen.getByRole('button', { name: '+ Add member' }));
      // The reused Bedrock-routing picker: platform roster, GitHub-username-first.
      await waitFor(() => expect(mockListPlatformUsers).toHaveBeenCalled());
      await waitFor(() => expect(screen.getByLabelText('Person')).not.toBeDisabled());
      expect(screen.getByText('newbie — new@studio.test (design-studio)')).toBeInTheDocument();

      await user.selectOptions(screen.getByLabelText('Person'), 'user-new');
      await user.selectOptions(screen.getByLabelText('Team'), 'sophos-team-web');
      await user.click(screen.getByRole('button', { name: 'Add member' }));

      // The T1 add-membership call — the same route the Teams dialog uses. With the
      // default "Member" role no role write fires at all.
      await waitFor(() =>
        expect(mockAddTeamMember).toHaveBeenCalledWith('sophos', 'sophos-team-web', {
          userId: 'user-new',
        })
      );
      expect(mockAssignUserRole).not.toHaveBeenCalled();
    });

    it('also assigns the org-admin role when that radio is chosen', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      await user.click(screen.getByRole('button', { name: '+ Add member' }));
      await waitFor(() => expect(screen.getByLabelText('Person')).not.toBeDisabled());
      await user.selectOptions(screen.getByLabelText('Person'), 'user-new');
      await user.selectOptions(screen.getByLabelText('Team'), 'sophos-team-web');
      await user.click(screen.getByRole('radio', { name: 'Org admin' }));
      await user.click(screen.getByRole('button', { name: 'Add member' }));

      await waitFor(() =>
        expect(mockAddTeamMember).toHaveBeenCalledWith('sophos', 'sophos-team-web', {
          userId: 'user-new',
        })
      );
      await waitFor(() =>
        expect(mockAssignUserRole).toHaveBeenCalledWith({
          user_id: 'user-new',
          role: 'org_admin',
          org_id: 'sophos',
        })
      );
    });

    it('hides "+ Add member" from an org admin, whose picker read would 403', async () => {
      // The membership write is ORG_UPDATE, but the person picker's roster read
      // (GET /admin/users, #4827) is platform-admin-only — the affordance follows
      // the narrowest read it depends on, per the panel's no-dead-end rule.
      mockPermissions.mockReturnValue(permissions({ isPlatformAdmin: () => false }));
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      expect(screen.queryByRole('button', { name: '+ Add member' })).not.toBeInTheDocument();
    });

    it('states roster truncation and loads the next page on demand', async () => {
      const PAGE2_USER = {
        id: 'user-kim',
        email: 'kim@sophos.test',
        name: 'Kim Lee',
        cognitoSub: 'sub-kim',
        role: 'member',
        githubUsername: 'kimlee',
      };
      mockGetOrgUsers
        .mockResolvedValueOnce({ ...ORG_USERS, total: 51, hasMore: true })
        .mockResolvedValueOnce({
          items: [PAGE2_USER],
          total: 51,
          page: 2,
          pageSize: 50,
          hasMore: false,
        });
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      // M4: member #51 must not be silently invisible — the cut is stated.
      expect(screen.getByText('Showing 1 of 51 members')).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Load more' }));

      // Page 2 of the roster AND of the budgets, appended to what is shown.
      await waitFor(() =>
        expect(mockGetOrgUsers).toHaveBeenCalledWith('sophos', { page: 2, pageSize: 50 })
      );
      await waitFor(() =>
        expect(mockGetMemberBudgets).toHaveBeenCalledWith('sophos', { page: 2, pageSize: 50 })
      );
      await waitFor(() => expect(screen.getByText('Kim Lee')).toBeInTheDocument());
      // Page 1 is still there (appended, not replaced) and the cut line is gone.
      expect(screen.getByText('Jane Doe')).toBeInTheDocument();
      expect(screen.queryByText(/Showing .* of .* members/)).not.toBeInTheDocument();
    });

    it('shows no truncation line when the roster fits one page', async () => {
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      expect(screen.queryByText(/Showing .* of .* members/)).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
    });

    it('warns in both team pickers when the org-wide team list is truncated', async () => {
      mockGetOrgTeams.mockResolvedValue({ ...ORG_TEAMS, total: 130, hasMore: true });
      const user = userEvent.setup();
      renderPanel();
      await openMembers(user);

      // The Teams dialog's picker…
      await openTeamsDialog(user);
      expect(screen.getByTestId('teams-truncated-warning')).toHaveTextContent(
        /Not every team is listed/
      );
      await user.click(screen.getByRole('button', { name: 'Done' }));

      // …and the assign-member modal's.
      await user.click(screen.getByRole('button', { name: '+ Add member' }));
      expect(screen.getByTestId('add-member-teams-truncated')).toHaveTextContent(
        /Not every team is listed/
      );
    });
  });
});
