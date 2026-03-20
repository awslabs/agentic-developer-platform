import { http, HttpResponse } from 'msw';
import { mockLogs } from '../data/logs';

export const logsHandlers = [
  http.get('/api/admin/logs', ({ request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');
    const orgId = url.searchParams.get('org_id');
    const userId = url.searchParams.get('user_id');
    const statusCode = url.searchParams.get('status_code');
    const pathPattern = url.searchParams.get('path_pattern');
    const minResponseTime = url.searchParams.get('min_response_time_ms');

    let filteredLogs = [...mockLogs];

    if (orgId) {
      filteredLogs = filteredLogs.filter((log) => log.org_id === orgId);
    }
    if (userId) {
      filteredLogs = filteredLogs.filter((log) => log.user_id === userId);
    }
    if (statusCode) {
      filteredLogs = filteredLogs.filter((log) => log.status_code === parseInt(statusCode));
    }
    if (pathPattern) {
      const regex = new RegExp(pathPattern.replace('*', '.*'));
      filteredLogs = filteredLogs.filter((log) => regex.test(log.path));
    }
    if (minResponseTime) {
      filteredLogs = filteredLogs.filter((log) => log.response_time_ms >= parseInt(minResponseTime));
    }

    // Sort by timestamp descending
    filteredLogs.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

    const start = (page - 1) * pageSize;
    const items = filteredLogs.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: filteredLogs.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < filteredLogs.length,
    });
  }),

  http.get('/api/admin/logs/:id', ({ params }) => {
    const log = mockLogs.find((l) => l.id === params.id);
    if (!log) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Log entry not found' },
        { status: 404 }
      );
    }
    return HttpResponse.json(log);
  }),

  http.get('/api/admin/logs/export', () => {
    // Generate CSV
    const headers = 'id,timestamp,org_id,user_id,method,path,status_code,response_time_ms\n';
    const rows = mockLogs
      .map((log) =>
        `${log.id},${log.timestamp},${log.org_id},${log.user_id},${log.method},${log.path},${log.status_code},${log.response_time_ms}`
      )
      .join('\n');
    const csv = headers + rows;

    return new HttpResponse(csv, {
      headers: {
        'Content-Type': 'text/csv',
        'Content-Disposition': 'attachment; filename="logs.csv"',
      },
    });
  }),
];
