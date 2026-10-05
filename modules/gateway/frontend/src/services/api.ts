import { deploymentSetting } from '@/config/runtime';
import type { ApiError, RequestOptions } from '@/types/api';
import { getAccessToken, clearTokens } from './auth';

const API_BASE_URL = deploymentSetting('VITE_API_URL') || '/api';

export class ApiClient {
  private baseUrl: string;
  private getToken: () => string | null;

  constructor(baseUrl: string = API_BASE_URL) {
    this.baseUrl = baseUrl;
    // Use Cognito access token for API authentication
    this.getToken = () => getAccessToken();
  }

  setTokenGetter(getter: () => string | null): void {
    this.getToken = getter;
  }

  private async handleResponse<T>(response: Response): Promise<T> {
    if (!response.ok) {
      let errorData: ApiError;
      try {
        errorData = await response.json();
      } catch {
        errorData = {
          error: 'Request failed',
          message: response.statusText || `HTTP ${response.status}`,
        };
      }

      if (response.status === 401) {
        // Clear tokens and redirect to login
        clearTokens();
        window.location.href = '/login';
      }

      // Attach the HTTP status to the thrown body.
      //
      // Without it a caller cannot tell a 403 from a 404 from a 503, because the
      // response body alone does not say: the gateway domain proxy, for one,
      // answers both "route not allowlisted" and "workspace not found" with the
      // same `{"detail": "Not found"}`. Those need different things from the
      // user — ask an administrator, wait for a deployment, retry — so a caller
      // that cannot distinguish them can only render one generic banner.
      //
      // Purely additive: the thrown value is still the parsed error body, and
      // existing callers reading `detail`/`message`/`error` are unaffected.
      errorData.status = response.status;

      throw errorData;
    }

    // Handle empty responses
    const text = await response.text();
    if (!text) {
      return {} as T;
    }

    try {
      return JSON.parse(text) as T;
    } catch {
      return {} as T;
    }
  }

  async request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
    const { method = 'GET', headers = {}, body, signal } = options;

    const token = this.getToken();
    const requestHeaders: Record<string, string> = {
      'Content-Type': 'application/json',
      ...headers,
    };

    if (token) {
      requestHeaders['Authorization'] = `Bearer ${token}`;
    }

    const config: RequestInit = {
      method,
      headers: requestHeaders,
      redirect: 'error',
      signal,
    };

    if (body && method !== 'GET') {
      config.body = JSON.stringify(body);
    }

    // nosemgrep: tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf — browser-side fetch of own API base; SSRF is not a client-side vulnerability
    const response = await fetch(`${this.baseUrl}${endpoint}`, config);
    return this.handleResponse<T>(response);
  }

  async get<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>(endpoint, { method: 'GET', signal });
  }

  async post<T>(endpoint: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(endpoint, { method: 'POST', body, signal });
  }

  async put<T>(endpoint: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(endpoint, { method: 'PUT', body, signal });
  }

  async patch<T>(endpoint: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(endpoint, { method: 'PATCH', body, signal });
  }

  async delete<T>(endpoint: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>(endpoint, { method: 'DELETE', body, signal });
  }
}

// Singleton instance
export const apiClient = new ApiClient();

// Helper to build query strings
export function buildQueryString(params: Record<string, unknown>): string {
  const searchParams = new URLSearchParams();

  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      searchParams.append(key, String(value));
    }
  }

  const queryString = searchParams.toString();
  return queryString ? `?${queryString}` : '';
}
