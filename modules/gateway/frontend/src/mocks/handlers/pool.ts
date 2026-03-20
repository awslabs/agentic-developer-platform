import { http, HttpResponse } from 'msw';
import { mockPoolAccounts } from '../data/organizations';

export const poolHandlers = [
  http.get('/api/admin/pool/status', () => {
    const healthyCount = mockPoolAccounts.filter((a) => a.is_healthy).length;
    return HttpResponse.json({
      total_accounts: mockPoolAccounts.length,
      healthy_accounts: healthyCount,
      unhealthy_accounts: mockPoolAccounts.length - healthyCount,
      accounts: mockPoolAccounts,
    });
  }),

  http.post('/api/admin/pool/accounts', async ({ request }) => {
    const body = await request.json() as {
      account_id: string;
      role_arn: string;
      region?: string;
    };
    const newAccount = {
      id: `pool-${Date.now()}`,
      account_id: body.account_id,
      role_arn: body.role_arn,
      region: body.region || 'us-east-1',
      is_healthy: true,
      last_health_check: new Date().toISOString(),
      created_at: new Date().toISOString(),
    };
    return HttpResponse.json(newAccount, { status: 201 });
  }),

  http.delete('/api/admin/pool/accounts/:accountId', () => {
    return HttpResponse.json({ success: true });
  }),

  http.post('/api/admin/pool/health-check', () => {
    // Simulate health check - update last_health_check timestamps
    const updatedAccounts = mockPoolAccounts.map((a) => ({
      ...a,
      last_health_check: new Date().toISOString(),
    }));
    const healthyCount = updatedAccounts.filter((a) => a.is_healthy).length;
    return HttpResponse.json({
      total_accounts: updatedAccounts.length,
      healthy_accounts: healthyCount,
      unhealthy_accounts: updatedAccounts.length - healthyCount,
      accounts: updatedAccounts,
    });
  }),
];
