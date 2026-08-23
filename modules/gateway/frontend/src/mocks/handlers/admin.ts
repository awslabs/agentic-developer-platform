import { http, HttpResponse } from 'msw';
import { mockOrganizations, mockDepartments, mockTeams } from '../data/organizations';
import { mockUsers } from '../data/users';

export const adminHandlers = [
  // Organizations
  http.get('/api/admin/organizations', ({ request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    const start = (page - 1) * pageSize;
    const items = mockOrganizations.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: mockOrganizations.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < mockOrganizations.length,
    });
  }),

  http.get('/api/admin/organizations/:id', ({ params }) => {
    const org = mockOrganizations.find((o) => o.id === params.id);
    if (!org) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Organization not found' },
        { status: 404 }
      );
    }
    return HttpResponse.json(org);
  }),

  http.post('/api/admin/organizations', async ({ request }) => {
    const body = await request.json() as { name: string };
    const newOrg = {
      id: `org-${Date.now()}`,
      name: body.name,
      aws_accounts: [],
      role_mappings: {},
      settings: {},
      created_at: new Date().toISOString(),
    };
    return HttpResponse.json(newOrg, { status: 201 });
  }),

  http.patch('/api/admin/organizations/:id', async ({ params, request }) => {
    const org = mockOrganizations.find((o) => o.id === params.id);
    if (!org) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Organization not found' },
        { status: 404 }
      );
    }
    const body = await request.json() as Record<string, unknown>;
    return HttpResponse.json({ ...org, ...body });
  }),

  http.delete('/api/admin/organizations/:id', ({ params }) => {
    const org = mockOrganizations.find((o) => o.id === params.id);
    if (!org) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Organization not found' },
        { status: 404 }
      );
    }
    return HttpResponse.json({ success: true });
  }),

  // Departments
  http.get('/api/admin/organizations/:orgId/departments', ({ params, request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    const depts = mockDepartments.filter((d) => d.org_id === params.orgId);
    const start = (page - 1) * pageSize;
    const items = depts.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: depts.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < depts.length,
    });
  }),

  http.post('/api/admin/organizations/:orgId/departments', async ({ params, request }) => {
    const body = await request.json() as { name: string; description?: string };
    const newDept = {
      id: `dept-${Date.now()}`,
      org_id: params.orgId as string,
      name: body.name,
      description: body.description,
      created_at: new Date().toISOString(),
    };
    return HttpResponse.json(newDept, { status: 201 });
  }),

  // Teams
  http.get('/api/admin/organizations/:orgId/departments/:deptId/teams', ({ params, request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    const teams = mockTeams.filter((t) => t.department_id === params.deptId);
    const start = (page - 1) * pageSize;
    const items = teams.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: teams.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < teams.length,
    });
  }),

  http.post('/api/admin/organizations/:orgId/departments/:deptId/teams', async ({ params, request }) => {
    const body = await request.json() as { name: string; description?: string };
    const newTeam = {
      id: `team-${Date.now()}`,
      department_id: params.deptId as string,
      name: body.name,
      description: body.description,
      created_at: new Date().toISOString(),
    };
    return HttpResponse.json(newTeam, { status: 201 });
  }),

  // Organization users endpoint (Issue #220)
  http.get('/api/admin/organizations/:orgId/users', ({ params, request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    // Filter users by org_id
    const users = mockUsers.filter((u) => u.org_id === params.orgId);
    const start = (page - 1) * pageSize;
    const items = users.slice(start, start + pageSize).map((u) => ({
      id: u.user_id,
      org_id: u.org_id,
      team_id: 'team-001', // Mock team assignment
      email: `${u.user_id}@example.com`,
      name: u.user_id.replace('user-', 'User '),
      role: u.role,
      cognito_sub: null,
      cognito_username: null,
      created_at: u.created_at,
      updated_at: u.created_at,
    }));

    return HttpResponse.json({
      items,
      total: users.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < users.length,
    });
  }),

  // Assignable roles for the current caller.
  // Issue #4019: this used to return a paginated USER list, which matches
  // neither the real endpoint nor either caller (getAvailableRoles and
  // chats.ts both read `{ roles: string[] }`) — the role picker rendered empty
  // in mock mode. The real endpoint is ceiling-filtered server-side; mock mode
  // signs in as a platform admin, so it returns the full set.
  http.get('/api/admin/users/roles', () => {
    return HttpResponse.json({
      roles: ['member', 'dept_admin', 'org_admin', 'platform_admin'],
    });
  }),

  // Role update. Issue #4019: replaces the POST/DELETE `/users/roles` stubs,
  // which pointed at endpoints the backend never had. "Remove role" is a PUT to
  // role=member here too — there is no delete path.
  http.put('/api/admin/organizations/:orgId/users/:userId', async ({ params, request }) => {
    const body = (await request.json()) as { role?: string };
    return HttpResponse.json({
      id: params.userId as string,
      org_id: params.orgId as string,
      team_id: 'team-001',
      email: `${params.userId}@example.com`,
      name: String(params.userId).replace('user-', 'User '),
      role: body.role ?? null,
      cognito_sub: null,
      cognito_username: null,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: new Date().toISOString(),
    });
  }),
];
