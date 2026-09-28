import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  getOrganizations,
  getOrganization,
  createOrganization,
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
  assignUserRole,
  removeUserRole,
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

    describe('assignUserRole', () => {
      it('throws error since it should be done via user management', async () => {
        // assignUserRole now throws an error directing to user management endpoints
        await expect(
          assignUserRole({
            user_id: 'user-1',
            role: AdminRole.ORG_ADMIN,
            org_id: 'org-1',
          })
        ).rejects.toThrow('User role assignment should be done via user management endpoints');
      });
    });

    describe('removeUserRole', () => {
      it('throws error since it should be done via user management', async () => {
        // removeUserRole now throws an error directing to user management endpoints
        await expect(removeUserRole('user-1')).rejects.toThrow(
          'User role removal should be done via user management endpoints'
        );
      });
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
