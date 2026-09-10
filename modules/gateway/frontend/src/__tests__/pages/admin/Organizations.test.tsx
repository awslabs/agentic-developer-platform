/**
 * Tests for the Organizations admin panel — Issue #4841 (#4839 · T2a).
 *
 * Covers the Validation section of the issue: creating an org with no GitHub involvement
 * (asserting the request targets the CANONICAL route and carries the caller-supplied
 * `id`), department CRUD, team CRUD, a non-admin seeing no panel, and a 403 from the
 * server surfacing as a readable message rather than a blank panel.
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
});
