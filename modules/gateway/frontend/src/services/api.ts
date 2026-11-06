import { deploymentSetting } from '@/config/runtime';
import type { ApiError, RequestOptions } from '@/types/api';
import { getAccessToken, clearTokens } from './auth';

const API_BASE_URL = deploymentSetting('VITE_API_URL') || '/api';

/** Safe, user-facing failures from retained-artifact verification. */
export class ArtifactVerificationError extends Error {}

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

  private async handleResponse<T>(response: Response, preserveSessionOnUnauthorized = false): Promise<T> {
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

      if (response.status === 401 && !preserveSessionOnUnauthorized) {
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

  private async send(endpoint: string, options: RequestOptions = {}): Promise<Response> {
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
    return fetch(`${this.baseUrl}${endpoint}`, config);
  }

  async requestVerifiedText(endpoint: string, sha256: string): Promise<string> {
    const response = await this.send(endpoint);
    if (!response.ok) return this.handleResponse<string>(response);
    if (!['text/x-diff', 'text/plain'].includes(response.headers.get('Content-Type')?.split(';')[0] ?? '')
      || response.headers.get('X-Adp-Content-Sha256') !== sha256) throw new Error('Artifact metadata does not match the retained patch.');
    const contents = await response.arrayBuffer();
    if (contents.byteLength > 1024 * 1024) throw new Error('Patch exceeds the in-browser inspection limit.');
    const digest = await crypto.subtle.digest('SHA-256', contents);
    const observed = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
    if (observed !== sha256) throw new Error('Artifact digest does not match the retained patch.');
    return new TextDecoder('utf-8', { fatal: true }).decode(contents);
  }

  async requestVerifiedJson(endpoint: string, reference: { sha256: string; bytes: number }, signal?: AbortSignal): Promise<unknown> {
    if (!/^[a-f0-9]{64}$/.test(reference.sha256) || !Number.isSafeInteger(reference.bytes)
      || reference.bytes < 1 || reference.bytes > 8 * 1024 * 1024) throw new ArtifactVerificationError('Report artifact identity is invalid.');
    const response = await this.send(endpoint, { signal });
    if (!response.ok) return this.handleResponse<unknown>(response);
    if (response.headers.get('Content-Type')?.split(';')[0] !== 'application/json'
      || response.headers.get('X-Adp-Content-Sha256') !== reference.sha256) throw new ArtifactVerificationError('Report artifact metadata changed.');
    const contents = await response.arrayBuffer();
    if (contents.byteLength !== reference.bytes) throw new ArtifactVerificationError('Report artifact length changed.');
    const digest = await crypto.subtle.digest('SHA-256', contents);
    const observed = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
    if (observed !== reference.sha256) throw new ArtifactVerificationError('Report artifact digest changed.');
    signal?.throwIfAborted();
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(contents));
  }

  async request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
    const response = await this.send(endpoint, options);
    return this.handleResponse<T>(response, options.preserveSessionOnUnauthorized);
  }

  async requestWithEtag<T>(endpoint: string, options: RequestOptions = {}): Promise<{ value: T; etag: string }> {
    const response = await this.send(endpoint, options);
    const value = await this.handleResponse<T>(response, options.preserveSessionOnUnauthorized);
    const etag = response.headers.get('ETag');
    if (!etag) throw new Error('The service did not return a revision token. Refresh before editing.');
    return { value, etag };
  }

  async get<T>(endpoint: string, signal?: AbortSignal, preserveSessionOnUnauthorized = false): Promise<T> {
    return this.request<T>(endpoint, { method: 'GET', signal, preserveSessionOnUnauthorized });
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
