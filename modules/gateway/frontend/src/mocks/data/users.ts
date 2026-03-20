import { AdminRole, Permission } from '@/types';

export const mockUsers = [
  {
    user_id: 'user-platform-admin-001',
    role: AdminRole.PLATFORM_ADMIN,
    org_id: null,
    dept_id: null,
    permissions: Object.values(Permission),
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    user_id: 'user-org-admin-001',
    role: AdminRole.ORG_ADMIN,
    org_id: 'org-001',
    dept_id: null,
    permissions: [
      Permission.ORG_READ,
      Permission.ORG_UPDATE,
      Permission.BUDGET_READ,
      Permission.BUDGET_UPDATE,
      Permission.RATELIMIT_READ,
      Permission.RATELIMIT_UPDATE,
      Permission.USAGE_READ,
      Permission.LOGS_READ,
      Permission.LOGS_EXPORT,
      Permission.USER_READ,
      Permission.USER_MANAGE,
    ],
    created_at: '2024-01-15T00:00:00Z',
  },
  {
    user_id: 'user-dept-admin-001',
    role: AdminRole.DEPT_ADMIN,
    org_id: 'org-001',
    dept_id: 'dept-001',
    permissions: [
      Permission.BUDGET_READ,
      Permission.RATELIMIT_READ,
      Permission.USAGE_READ,
      Permission.LOGS_READ,
      Permission.USER_READ,
    ],
    created_at: '2024-02-01T00:00:00Z',
  },
];

export const currentUser = mockUsers[0]; // Default to platform admin for testing
