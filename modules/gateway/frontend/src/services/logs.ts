import { apiClient, buildQueryString } from './api';
import type { PaginatedResponse } from '@/types/api';
import type { LogEntry, LogQueryRequest } from '@/types';

export async function getLogs(params?: LogQueryRequest): Promise<PaginatedResponse<LogEntry>> {
  const query = buildQueryString({
    start_time: params?.start_time,
    end_time: params?.end_time,
    org_id: params?.org_id,
    user_id: params?.user_id,
    status_code: params?.status_code,
    path_pattern: params?.path_pattern,
    min_response_time_ms: params?.min_response_time_ms,
    page: params?.page || 1,
    page_size: params?.page_size || 50,
  });

  const response = await apiClient.get<{
    items: Array<{
      id: string;
      timestamp: string;
      org_id: string;
      user_id: string;
      method: string;
      path: string;
      status_code: number;
      response_time_ms: number;
      request_body_size: number | null;
      response_body_size: number | null;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/logs${query}`);

  return {
    items: response.items.map(transformLogEntry),
    total: response.total,
    page: response.page,
    pageSize: response.page_size,
    hasMore: response.has_more,
  };
}

export async function getLogEntry(id: string): Promise<LogEntry> {
  const response = await apiClient.get<{
    id: string;
    timestamp: string;
    org_id: string;
    user_id: string;
    method: string;
    path: string;
    status_code: number;
    response_time_ms: number;
    request_body_size: number | null;
    response_body_size: number | null;
  }>(`/admin/logs/${id}`);

  return transformLogEntry(response);
}

export async function exportLogs(params?: LogQueryRequest): Promise<Blob> {
  const query = buildQueryString({
    start_time: params?.start_time,
    end_time: params?.end_time,
    org_id: params?.org_id,
    user_id: params?.user_id,
    status_code: params?.status_code,
    path_pattern: params?.path_pattern,
    min_response_time_ms: params?.min_response_time_ms,
    format: 'csv',
  });

  const response = await fetch(`/api/admin/logs/export${query}`, {
    headers: {
      Authorization: `Bearer ${sessionStorage.getItem('auth_token')}`,
    },
  });

  if (!response.ok) {
    throw new Error('Failed to export logs');
  }

  return response.blob();
}

export function downloadLogs(blob: Blob, filename: string = 'logs.csv'): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function transformLogEntry(data: {
  id: string;
  timestamp: string;
  org_id: string;
  user_id: string;
  method: string;
  path: string;
  status_code: number;
  response_time_ms: number;
  request_body_size: number | null;
  response_body_size: number | null;
}): LogEntry {
  return {
    id: data.id,
    timestamp: data.timestamp,
    orgId: data.org_id,
    userId: data.user_id,
    method: data.method,
    path: data.path,
    statusCode: data.status_code,
    responseTimeMs: data.response_time_ms,
    requestBodySize: data.request_body_size,
    responseBodySize: data.response_body_size,
  };
}
