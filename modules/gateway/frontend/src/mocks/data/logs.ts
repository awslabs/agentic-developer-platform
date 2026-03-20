export function generateMockLogs(count: number = 100) {
  const methods = ['GET', 'POST', 'PUT', 'DELETE'];
  const paths = [
    '/v1/chat/completions',
    '/v1/completions',
    '/v1/embeddings',
    '/v1/models',
    '/api/v1/messages',
  ];
  const statusCodes = [200, 200, 200, 200, 200, 201, 400, 401, 403, 429, 500];
  const orgIds = ['org-001', 'org-002', 'org-003'];
  const userIds = ['user-001', 'user-002', 'user-003', 'user-004', 'user-005'];

  return Array.from({ length: count }, (_, i) => ({
    id: `log-${String(i + 1).padStart(6, '0')}`,
    timestamp: new Date(Date.now() - Math.random() * 7 * 24 * 60 * 60 * 1000).toISOString(),
    org_id: orgIds[Math.floor(Math.random() * orgIds.length)],
    user_id: userIds[Math.floor(Math.random() * userIds.length)],
    method: methods[Math.floor(Math.random() * methods.length)],
    path: paths[Math.floor(Math.random() * paths.length)],
    status_code: statusCodes[Math.floor(Math.random() * statusCodes.length)],
    response_time_ms: Math.floor(Math.random() * 2000) + 50,
    request_body_size: Math.floor(Math.random() * 10000),
    response_body_size: Math.floor(Math.random() * 50000),
  }));
}

export const mockLogs = generateMockLogs(100);
