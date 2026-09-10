import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getOrganizations,
  getOrganization,
  createOrganization,
  createOrganizationCanonical,
  updateOrganization,
  deleteOrganization,
  getDepartments,
  createDepartment,
  updateDepartment,
  deleteDepartment,
  getTeams,
  createTeam,
  updateTeam,
  deleteTeam,
  getUserRoles,
  getAvailableRoles,
  assignUserRole,
  removeUserRole,
  getMemberGithubUserId,
  listPlatformUsers,
} from '@/services/admin';
import { AdminRole } from '@/types';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  buildQueryString: vi.fn((params) => {
    const searchParams = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') {
        searchParams.append(key, String(value));
      }
    }
    const queryString = searchParams.toString();
    return queryString ? `?${queryString}` : '';
  }),
}));

describe('Admin Service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Organizations', () => {
    describe('getOrganizations', () => {
      it('fetches organizations with default pagination', async () => {
        const mockResponse = {
          items: [
            {
              id: 'org-1',
              name: 'Org 1',
              aws_accounts: ['123456789012'],
              role_mappings: {},
              settings: {},
              created_at: '2024-01-01T00:00:00Z',
            },
          ],
          total: 1,
          page: 1,
          page_size: 50,
          has_more: false,
        };

        vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

        const result = await getOrganizations();

        expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/organizations'));
        expect(result.items).toHaveLength(1);
        expect(result.items[0].id).toBe('org-1');
        expect(result.items[0].awsAccounts).toEqual(['123456789012']);
        expect(result.total).toBe(1);
      });

      it('fetches organizations with custom pagination', async () => {
        const mockResponse = {
          items: [],
          total: 0,
          page: 2,
          page_size: 10,
          has_more: false,
        };

        vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

        await getOrganizations({ page: 2, pageSize: 10 });

        expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining('/admin/organizations'));
      });
    });

    describe('getOrganization', () => {
      it('fetches a single organization by ID', async () => {
        const mockResponse = {
          id: 'org-1',
          name: 'Test Org',
          aws_accounts: ['123456789012'],
          role_mappings: { admin: 'arn:aws:iam::123456789012:role/Admin' },
          settings: { feature_x: true },
          created_at: '2024-01-01T00:00:00Z',
        };

        vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

        const result = await getOrganization('org-1');

        expect(apiClient.get).toHaveBeenCalledWith('/admin/organizations/org-1');
        expect(result.id).toBe('org-1');
        expect(result.name).toBe('Test Org');
        expect(result.roleMappings).toEqual({ admin: 'arn:aws:iam::123456789012:role/Admin' });
      });
    });

    describe('createOrganization', () => {
      it('creates a new organization', async () => {
        const mockResponse = {
          id: 'org-new',
          name: 'New Org',
          aws_accounts: [],
          role_mappings: {},
          settings: {},
          created_at: '2024-01-01T00:00:00Z',
        };

        vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

        const result = await createOrganization({ name: 'New Org' });

        expect(apiClient.post).toHaveBeenCalledWith('/admin/organizations', { name: 'New Org' });
        expect(result.id).toBe('org-new');
        expect(result.name).toBe('New Org');
      });
    });

    describe('createOrganizationCanonical', () => {
      it('posts the caller-supplied id to the canonical identity route', async () => {
        vi.mocked(apiClient.post).mockResolvedValue({ id: 'acme-corp', name: 'Acme Corp' });

        const result = await createOrganizationCanonical({ id: 'acme-corp', name: 'Acme Corp' });

        // The `/api` IS doubled on purpose (Issue #4841). apiClient's base is already
        // `/api` and the identity router mounts at `/api/admin/identity`, so the browser
        // emits `/api/api/...` and the CloudFront viewer function strips one segment back
        // to the mount. Dropping one `/api` here 404s in a deployed environment; this
        // assertion is what catches a well-meaning "cleanup" of the duplicate.
        expect(apiClient.post).toHaveBeenCalledWith('/api/admin/identity/organizations', {
          id: 'acme-corp',
          name: 'Acme Corp',
        });
        expect(result).toEqual({ id: 'acme-corp', name: 'Acme Corp' });
      });

      it('sends the id even when it differs from a slug of the name', async () => {
        // The route requires an id and never derives one, so whatever the admin confirmed
        // must reach the server verbatim.
        vi.mocked(apiClient.post).mockResolvedValue({ id: 'acme-emea', name: 'Acme Corp' });

        await createOrganizationCanonical({ id: 'acme-emea', name: 'Acme Corp' });

        expect(apiClient.post).toHaveBeenCalledWith('/api/admin/identity/organizations', {
          id: 'acme-emea',
          name: 'Acme Corp',
        });
      });
    });

    describe('updateOrganization', () => {
      it('updates an existing organization', async () => {
        const mockResponse = {
          id: 'org-1',
          name: 'Updated Org',
          aws_accounts: ['new-account'],
          role_mappings: {},
          settings: {},
          created_at: '2024-01-01T00:00:00Z',
        };

        vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

        const result = await updateOrganization('org-1', { name: 'Updated Org' });

        expect(apiClient.put).toHaveBeenCalledWith('/admin/organizations/org-1', {
          name: 'Updated Org',
        });
        expect(result.name).toBe('Updated Org');
      });
    });

    describe('deleteOrganization', () => {
      it('deletes an organization', async () => {
        vi.mocked(apiClient.delete).mockResolvedValue({});

        await deleteOrganization('org-1');

        expect(apiClient.delete).toHaveBeenCalledWith('/admin/organizations/org-1');
      });
    });
  });

  describe('Departments', () => {
    describe('getDepartments', () => {
      it('fetches departments for an organization', async () => {
        const mockResponse = {
          items: [
            {
              id: 'dept-1',
              org_id: 'org-1',
              name: 'Engineering',
              description: 'Engineering department',
              created_at: '2024-01-01T00:00:00Z',
            },
          ],
          total: 1,
          page: 1,
          page_size: 50,
          has_more: false,
        };

        vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

        const result = await getDepartments('org-1');

        expect(apiClient.get).toHaveBeenCalledWith(
          expect.stringContaining('/admin/organizations/org-1/departments')
        );
        expect(result.items).toHaveLength(1);
        expect(result.items[0].orgId).toBe('org-1');
      });
    });

    describe('createDepartment', () => {
      it('creates a new department', async () => {
        const mockResponse = {
          id: 'dept-new',
          org_id: 'org-1',
          name: 'Sales',
          description: 'Sales team',
          created_at: '2024-01-01T00:00:00Z',
        };

        vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

        const result = await createDepartment('org-1', {
          name: 'Sales',
          description: 'Sales team',
        });

        expect(apiClient.post).toHaveBeenCalledWith('/admin/organizations/org-1/departments', {
          name: 'Sales',
          description: 'Sales team',
        });
        expect(result.name).toBe('Sales');
      });
    });

    describe('updateDepartment', () => {
      it('updates an existing department', async () => {
        const mockResponse = {
          id: 'dept-1',
          org_id: 'org-1',
          name: 'Updated Dept',
          description: 'Updated description',
          created_at: '2024-01-01T00:00:00Z',
        };

        vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

        const result = await updateDepartment('org-1', 'dept-1', { name: 'Updated Dept' });

        expect(apiClient.put).toHaveBeenCalledWith(
          '/admin/organizations/org-1/departments/dept-1',
          { name: 'Updated Dept' }
        );
        expect(result.name).toBe('Updated Dept');
      });
    });

    describe('deleteDepartment', () => {
      it('deletes a department', async () => {
        vi.mocked(apiClient.delete).mockResolvedValue({});

        await deleteDepartment('org-1', 'dept-1');

        expect(apiClient.delete).toHaveBeenCalledWith(
          '/admin/organizations/org-1/departments/dept-1'
        );
      });
    });
  });

  describe('Teams', () => {
    describe('getTeams', () => {
      it('fetches teams for a department', async () => {
        const mockResponse = {
          items: [
            {
              id: 'team-1',
              department_id: 'dept-1',
              name: 'Frontend',
              description: 'Frontend team',
              created_at: '2024-01-01T00:00:00Z',
            },
          ],
          total: 1,
          page: 1,
          page_size: 50,
          has_more: false,
        };

        vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

        const result = await getTeams('org-1', 'dept-1');

        expect(apiClient.get).toHaveBeenCalledWith(
          expect.stringContaining('/admin/organizations/org-1/departments/dept-1/teams')
        );
        expect(result.items).toHaveLength(1);
        expect(result.items[0].departmentId).toBe('dept-1');
      });
    });

    describe('createTeam', () => {
      it('creates a new team', async () => {
        const mockResponse = {
          id: 'team-new',
          department_id: 'dept-1',
          name: 'Backend',
          description: 'Backend team',
          created_at: '2024-01-01T00:00:00Z',
        };

        vi.mocked(apiClient.post).mockResolvedValue(mockResponse);

        const result = await createTeam('org-1', 'dept-1', {
          name: 'Backend',
          description: 'Backend team',
        });

        expect(apiClient.post).toHaveBeenCalledWith(
          '/admin/organizations/org-1/departments/dept-1/teams',
          { name: 'Backend', description: 'Backend team' }
        );
        expect(result.name).toBe('Backend');
      });
    });

    describe('updateTeam', () => {
      it('updates an existing team', async () => {
        const mockResponse = {
          id: 'team-1',
          org_id: 'org-1',
          department_id: 'dept-1',
          name: 'Updated Team',
          created_at: '2024-01-01T00:00:00Z',
          updated_at: '2024-01-02T00:00:00Z',
        };

        vi.mocked(apiClient.put).mockResolvedValue(mockResponse);

        // Note: updateTeam signature changed to (orgId, teamId, data) per backend API
        const result = await updateTeam('org-1', 'team-1', { name: 'Updated Team' });

        expect(apiClient.put).toHaveBeenCalledWith(
          '/admin/organizations/org-1/teams/team-1',
          { name: 'Updated Team' }
        );
        expect(result.name).toBe('Updated Team');
      });
    });

    describe('deleteTeam', () => {
      it('deletes a team', async () => {
        vi.mocked(apiClient.delete).mockResolvedValue({});

        // Note: deleteTeam signature changed to (orgId, teamId) per backend API
        await deleteTeam('org-1', 'team-1');

        expect(apiClient.delete).toHaveBeenCalledWith(
          '/admin/organizations/org-1/teams/team-1'
        );
      });
    });
  });

  describe('User Roles', () => {
    describe('getUserRoles', () => {
      it('fetches user roles for an organization', async () => {
        // getUserRoles now uses /admin/organizations/{org_id}/users endpoint
        const mockResponse = {
          items: [
            {
              id: 'user-1',
              org_id: 'org-1',
              team_id: 'team-1',
              email: 'user@example.com',
              name: 'Test User',
              role: 'org_admin',
              cognito_sub: null,
              cognito_username: null,
              created_at: '2024-01-01T00:00:00Z',
              updated_at: '2024-01-01T00:00:00Z',
            },
          ],
          total: 1,
          page: 1,
          page_size: 50,
          has_more: false,
        };

        vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

        const result = await getUserRoles('org-1');

        expect(apiClient.get).toHaveBeenCalledWith(
          expect.stringContaining('/admin/organizations/org-1/users')
        );
        expect(result.items).toHaveLength(1);
        expect(result.items[0].userId).toBe('user-1');
      });

      it('returns empty result when no org_id provided', async () => {
        // Without org_id, getUserRoles returns empty result
        const result = await getUserRoles();

        expect(apiClient.get).not.toHaveBeenCalled();
        expect(result.items).toHaveLength(0);
        expect(result.total).toBe(0);
      });
    });

    describe('getAvailableRoles', () => {
      it('returns the roles the backend says this caller may assign', async () => {
        vi.mocked(apiClient.get).mockResolvedValue({ roles: ['member', 'dept_admin', 'org_admin'] });

        const result = await getAvailableRoles();

        expect(apiClient.get).toHaveBeenCalledWith('/admin/users/roles');
        expect(result).toEqual(['member', 'dept_admin', 'org_admin']);
      });
    });

    // Issue #4019: these two used to assert `rejects.toThrow(...)` placeholders.
    describe('assignUserRole', () => {
      it('PUTs the role to the org-scoped user endpoint', async () => {
        vi.mocked(apiClient.put).mockResolvedValue({
          id: 'user-1',
          org_id: 'org-1',
          role: 'org_admin',
          created_at: '2024-01-01T00:00:00Z',
        });

        const result = await assignUserRole({
          user_id: 'user-1',
          role: AdminRole.ORG_ADMIN,
          org_id: 'org-1',
        });

        expect(apiClient.put).toHaveBeenCalledWith('/admin/organizations/org-1/users/user-1', {
          role: 'org_admin',
        });
        expect(result.userId).toBe('user-1');
        expect(result.role).toBe('org_admin');
        expect(result.orgId).toBe('org-1');
      });

      it('rejects without an org_id instead of calling an unscoped endpoint', async () => {
        // The endpoint is org-scoped; a call without org_id would hit
        // /admin/organizations/undefined/... and 404 with a confusing message.
        await expect(
          assignUserRole({ user_id: 'user-1', role: AdminRole.ORG_ADMIN })
        ).rejects.toThrow('org_id is required');
        expect(apiClient.put).not.toHaveBeenCalled();
      });
    });

    describe('removeUserRole', () => {
      it('demotes to member rather than deleting the membership', async () => {
        vi.mocked(apiClient.put).mockResolvedValue({});

        await removeUserRole('user-1', 'org-1');

        // A DELETE would leave a no-row principal; the contract is "demote".
        expect(apiClient.delete).not.toHaveBeenCalled();
        expect(apiClient.put).toHaveBeenCalledWith('/admin/organizations/org-1/users/user-1', {
          role: 'member',
        });
      });

      it('rejects without an org_id', async () => {
        await expect(removeUserRole('user-1')).rejects.toThrow('org_id is required');
        expect(apiClient.put).not.toHaveBeenCalled();
      });
    });
  });

  // Issue #4687: the only server-side source of a member's GitHub numeric id, which is
  // what the cross-workspace person anchor (`github:<id>`) is built from. Everything
  // here exists to keep that id from being inferred client-side: a cap keyed on a guess
  // validates, displays a number, and is never matched by enforcement (#4511).
  describe('getMemberGithubUserId', () => {
    it('reads the id from the identities endpoint', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        identities: [
          { provider: 'github', provider_user_id: '20402445' },
        ],
        total: 1,
      });

      const id = await getMemberGithubUserId('user-operator');

      expect(id).toBe('20402445');
      // The doubled `/api` is deliberate: CloudFront strips the first segment, and this
      // router is one of the quarantined `/api`-prefixed mounts. Dropping one 404s.
      expect(apiClient.get).toHaveBeenCalledWith(
        '/api/admin/identity/users/user-operator/identities'
      );
    });

    it('picks the github identity out of several providers', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        identities: [
          { provider: 'google', provider_user_id: 'not-a-github-id' },
          { provider: 'github', provider_user_id: '20402445' },
        ],
        total: 2,
      });

      // Provider-matched rather than positional: an anchor built from another
      // provider's id would be a well-formed key for a person who does not exist.
      expect(await getMemberGithubUserId('user-operator')).toBe('20402445');
    });

    it('returns null for a member with no linked identities', async () => {
      // Legitimate and permanent for someone who signed up by email. Null rather than a
      // throw, because the caller has a real answer to give ("no limit can be set for
      // this person") and an exception would render it as an outage.
      vi.mocked(apiClient.get).mockResolvedValue({ identities: [], total: 0 });

      expect(await getMemberGithubUserId('user-invited')).toBeNull();
    });

    it('returns null when linked to other providers but not github', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        identities: [{ provider: 'google', provider_user_id: '123' }],
        total: 1,
      });

      expect(await getMemberGithubUserId('user-invited')).toBeNull();
    });

    it('returns null for a blank provider id rather than a bare prefix', async () => {
      // `github:` with nothing after it is an anchor the server rejects, so treating a
      // blank as "no identity" keeps the caller from submitting a key it knows is bad.
      vi.mocked(apiClient.get).mockResolvedValue({
        identities: [{ provider: 'github', provider_user_id: '   ' }],
        total: 1,
      });

      expect(await getMemberGithubUserId('user-operator')).toBeNull();
    });

    it('propagates transport and authorization failures', async () => {
      // A 403 or a network fault is NOT "this person has no GitHub identity". Collapsing
      // the two would let a permissions problem read as a fact about the member.
      vi.mocked(apiClient.get).mockRejectedValue(new Error('Forbidden'));

      await expect(getMemberGithubUserId('user-operator')).rejects.toThrow('Forbidden');
    });
  });

  // Issue #4827: the source the person-scoped admin pickers read. The routing panel used
  // to ask an operator to type a `users.id` by hand, which nobody could produce. What
  // matters here is the shape of the request (a *server-side* search, so the browser
  // never pulls the whole member table) and that the canonical id survives the
  // snake_case→camelCase hop intact.
  describe('listPlatformUsers', () => {
    it('camelCases the roster and keeps the canonical id', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        items: [
          {
            id: '48270000-0000-4000-8000-00000000ca5e',
            org_id: 'org-acme',
            email: 'casey@acme.example',
            name: 'Casey Ng',
            github_username: 'caseyng',
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      });

      const result = await listPlatformUsers({ q: 'casey' });

      // Search reaches the server. A client-side filter is what pagination exists to
      // avoid on a table that grows with every signup.
      expect(apiClient.get).toHaveBeenCalledWith('/admin/users?q=casey&page=1&page_size=50');
      // The id is what a routing rule is stored under (#4647), so it must pass through
      // untouched — an email or a login here would store cleanly and govern nobody.
      expect(result.items[0].id).toBe('48270000-0000-4000-8000-00000000ca5e');
      expect(result.items[0].githubUsername).toBe('caseyng');
      expect(result.items[0].orgId).toBe('org-acme');
      expect(result.hasMore).toBe(false);
    });

    it('keeps a null github username null rather than blanking it', async () => {
      // An email-onboarded member has no GitHub login, permanently. Coercing that to ''
      // would make the picker label them as though the lookup had failed.
      vi.mocked(apiClient.get).mockResolvedValue({
        items: [
          {
            id: 'user-invited',
            org_id: 'org-acme',
            email: 'invited@acme.example',
            name: null,
            github_username: null,
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        has_more: false,
      });

      const result = await listPlatformUsers();

      expect(result.items[0].githubUsername).toBeNull();
      expect(result.items[0].name).toBeNull();
    });

    it('omits a blank search instead of filtering on the empty string', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50, has_more: false });

      await listPlatformUsers({ q: '   ' });

      // A cleared search box must reset to everybody. Sending `q=` (or `q=%20`) would
      // ask the server to match whitespace and return an empty picker.
      expect(apiClient.get).toHaveBeenCalledWith('/admin/users?page=1&page_size=50');
    });
  });

  describe('Error Handling', () => {
    it('propagates API errors', async () => {
      const error = { error: 'Not Found', message: 'Organization not found' };
      vi.mocked(apiClient.get).mockRejectedValue(error);

      await expect(getOrganization('invalid-id')).rejects.toEqual(error);
    });

    it('handles network errors', async () => {
      vi.mocked(apiClient.post).mockRejectedValue(new Error('Network error'));

      await expect(createOrganization({ name: 'Test' })).rejects.toThrow('Network error');
    });
  });
});
