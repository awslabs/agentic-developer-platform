import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiClient } from '@/services/api';
import {
  generatePKCEChallenge,
  storePKCEVerifier,
  getPKCEVerifier,
  buildLoginUrl,
  buildLogoutUrl,
  exchangeCodeForTokens,
  refreshAccessToken,
  storeTokens,
  getAccessToken,
  getIdToken,
  getRefreshToken,
  getTokenExpiry,
  clearTokens,
  parseTokenPayload,
  isTokenExpired,
  parseIdTokenForUser,
  getCurrentUser,
  logout,
  handleOAuthCallback,
  refreshToken,
} from '@/services/auth';
import { AdminRole, Permission } from '@/types';
import type { CognitoTokenResponse, CognitoIdTokenPayload } from '@/types';

// Mock the API client
vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

// Mock cognito config
vi.mock('@/config/cognito', () => ({
  getCognitoConfig: () => ({
    userPoolId: 'us-east-1_testpool',
    clientId: 'test-client-id',
    domain: 'test-domain',
    region: 'us-east-1',
    redirectUri: 'http://localhost:5173/auth/callback',
  }),
  getCognitoAuthorizeUrl: () => 'https://test-domain.auth.us-east-1.amazoncognito.com/oauth2/authorize',
  getCognitoTokenUrl: () => 'https://test-domain.auth.us-east-1.amazoncognito.com/oauth2/token',
  getCognitoLogoutUrl: () => 'https://test-domain.auth.us-east-1.amazoncognito.com/logout',
  isCognitoConfigured: () => true,
}));

// Mock fetch for token exchange.
// NOTE: assign inside beforeEach, not at module scope. The MSW server's
// server.listen() (in src/test/setup.ts beforeAll) replaces globalThis.fetch
// with its interceptor AFTER this module is evaluated, so a module-level
// assignment gets clobbered. Re-installing per test ensures our mock wins.
const mockFetch = vi.fn();

describe('Auth Service - OAuth PKCE', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    mockFetch.mockReset();
    (globalThis as unknown as { fetch: typeof fetch }).fetch = mockFetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('PKCE Challenge Generation', () => {
    it('generates a PKCE challenge with verifier and challenge', async () => {
      const pkce = await generatePKCEChallenge();

      expect(pkce).toHaveProperty('verifier');
      expect(pkce).toHaveProperty('challenge');
      expect(pkce.verifier).toHaveLength(64);
      expect(pkce.challenge).toBeDefined();
      // Challenge should be base64url encoded
      expect(pkce.challenge).toMatch(/^[A-Za-z0-9_-]+$/);
    });

    it('generates unique challenges each time', async () => {
      const pkce1 = await generatePKCEChallenge();
      const pkce2 = await generatePKCEChallenge();

      expect(pkce1.verifier).not.toBe(pkce2.verifier);
      expect(pkce1.challenge).not.toBe(pkce2.challenge);
    });
  });

  describe('PKCE Verifier Storage', () => {
    it('stores and retrieves PKCE verifier', () => {
      const verifier = 'test-verifier-string';
      storePKCEVerifier(verifier);

      const retrieved = getPKCEVerifier();
      expect(retrieved).toBe(verifier);
    });

    it('clears verifier after retrieval', () => {
      storePKCEVerifier('test-verifier');
      getPKCEVerifier();

      const secondRetrieval = getPKCEVerifier();
      expect(secondRetrieval).toBeNull();
    });

    it('returns null when no verifier stored', () => {
      const result = getPKCEVerifier();
      expect(result).toBeNull();
    });
  });

  describe('Login URL Building', () => {
    it('builds login URL with PKCE parameters', async () => {
      const loginUrl = await buildLoginUrl();

      expect(loginUrl).toContain('oauth2/authorize');
      expect(loginUrl).toContain('response_type=code');
      expect(loginUrl).toContain('client_id=test-client-id');
      expect(loginUrl).toContain('redirect_uri=');
      expect(loginUrl).toContain('scope=openid+email+profile');
      expect(loginUrl).toContain('code_challenge=');
      expect(loginUrl).toContain('code_challenge_method=S256');
    });

    it('stores PKCE verifier when building login URL', async () => {
      await buildLoginUrl();

      // Verifier should be stored in sessionStorage
      const storedVerifier = sessionStorage.getItem('pkce_code_verifier');
      expect(storedVerifier).toBeDefined();
      expect(storedVerifier).toHaveLength(64);
    });
  });

  describe('Logout URL Building', () => {
    it('builds logout URL with client_id and logout_uri', () => {
      const logoutUrl = buildLogoutUrl();

      expect(logoutUrl).toContain('logout');
      expect(logoutUrl).toContain('client_id=test-client-id');
      expect(logoutUrl).toContain('logout_uri=');
    });
  });

  describe('Token Exchange', () => {
    it('exchanges authorization code for tokens', async () => {
      const mockTokenResponse: CognitoTokenResponse = {
        access_token: 'mock-access-token',
        id_token: 'mock-id-token',
        refresh_token: 'mock-refresh-token',
        expires_in: 3600,
        token_type: 'Bearer',
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockTokenResponse),
      });

      const result = await exchangeCodeForTokens('auth-code', 'verifier');

      expect(mockFetch).toHaveBeenCalledWith(
        'https://test-domain.auth.us-east-1.amazoncognito.com/oauth2/token',
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        })
      );
      expect(result).toEqual(mockTokenResponse);
    });

    it('throws error on failed token exchange', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        json: () => Promise.resolve({ error_description: 'Invalid code' }),
      });

      await expect(exchangeCodeForTokens('invalid-code', 'verifier')).rejects.toThrow(
        'Invalid code'
      );
    });
  });

  describe('Token Refresh', () => {
    it('refreshes access token using refresh token', async () => {
      const mockTokenResponse: CognitoTokenResponse = {
        access_token: 'new-access-token',
        id_token: 'new-id-token',
        refresh_token: 'mock-refresh-token',
        expires_in: 3600,
        token_type: 'Bearer',
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockTokenResponse),
      });

      const result = await refreshAccessToken('mock-refresh-token');

      expect(mockFetch).toHaveBeenCalled();
      expect(result.access_token).toBe('new-access-token');
    });

    it('throws error on failed token refresh', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        json: () => Promise.resolve({ error_description: 'Refresh token expired' }),
      });

      await expect(refreshAccessToken('expired-refresh-token')).rejects.toThrow(
        'Refresh token expired'
      );
    });
  });

  describe('Token Storage', () => {
    const mockTokens: CognitoTokenResponse = {
      access_token: 'test-access-token',
      id_token: 'test-id-token',
      refresh_token: 'test-refresh-token',
      expires_in: 3600,
      token_type: 'Bearer',
    };

    it('stores all tokens in sessionStorage', () => {
      storeTokens(mockTokens);

      expect(getAccessToken()).toBe('test-access-token');
      expect(getIdToken()).toBe('test-id-token');
      expect(getRefreshToken()).toBe('test-refresh-token');
      expect(getTokenExpiry()).toBeGreaterThan(Date.now());
    });

    it('clears all tokens from sessionStorage', () => {
      storeTokens(mockTokens);
      clearTokens();

      expect(getAccessToken()).toBeNull();
      expect(getIdToken()).toBeNull();
      expect(getRefreshToken()).toBeNull();
      expect(getTokenExpiry()).toBeNull();
    });
  });

  describe('Token Expiration Check', () => {
    it('returns true when no token expiry is stored', () => {
      expect(isTokenExpired()).toBe(true);
    });

    it('returns true when token is expired', () => {
      const expiredTime = Date.now() - 1000;
      sessionStorage.setItem('cognito_token_expiry', expiredTime.toString());

      expect(isTokenExpired()).toBe(true);
    });

    it('returns true when token expires within buffer time', () => {
      const almostExpired = Date.now() + 3 * 60 * 1000; // 3 minutes (default buffer is 5)
      sessionStorage.setItem('cognito_token_expiry', almostExpired.toString());

      expect(isTokenExpired()).toBe(true);
    });

    it('returns false when token has time remaining', () => {
      const validTime = Date.now() + 10 * 60 * 1000; // 10 minutes
      sessionStorage.setItem('cognito_token_expiry', validTime.toString());

      expect(isTokenExpired()).toBe(false);
    });
  });

  describe('Token Parsing', () => {
    it('parses valid JWT payload', () => {
      const payload = { sub: 'user-123', email: 'test@example.com' };
      const base64Payload = btoa(JSON.stringify(payload));
      const token = `header.${base64Payload}.signature`;

      const result = parseTokenPayload<typeof payload>(token);

      expect(result).toEqual(payload);
    });

    it('returns null for invalid token format', () => {
      expect(parseTokenPayload('invalid')).toBeNull();
      expect(parseTokenPayload('only.two')).toBeNull();
    });

    it('returns null for malformed payload', () => {
      const token = 'header.not-valid-base64.signature';
      expect(parseTokenPayload(token)).toBeNull();
    });
  });

  describe('ID Token to User Parsing', () => {
    it('parses Cognito ID token to User object', () => {
      const payload: Partial<CognitoIdTokenPayload> = {
        sub: 'user-123',
        email: 'test@example.com',
        name: 'Test User',
        'cognito:username': 'testuser',
        'custom:org_id': 'org-456',
        'custom:department_id': 'dept-789',
        'custom:team_id': 'team-001',
        'custom:role': 'org_admin',
        iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test',
        aud: 'test-client-id',
        exp: Math.floor(Date.now() / 1000) + 3600,
        iat: Math.floor(Date.now() / 1000),
        auth_time: Math.floor(Date.now() / 1000),
        token_use: 'id',
      };
      const base64Payload = btoa(JSON.stringify(payload));
      const token = `header.${base64Payload}.signature`;

      const user = parseIdTokenForUser(token);

      expect(user).not.toBeNull();
      expect(user?.id).toBe('user-123');
      expect(user?.email).toBe('test@example.com');
      expect(user?.name).toBe('Test User');
      expect(user?.orgId).toBe('org-456');
      expect(user?.deptId).toBe('dept-789');
      expect(user?.role).toBe(AdminRole.ORG_ADMIN);
    });

    it('assigns platform admin role for platform_admin custom role', () => {
      const payload: Partial<CognitoIdTokenPayload> = {
        sub: 'admin-user',
        email: 'admin@example.com',
        'cognito:username': 'admin',
        'custom:role': 'platform_admin',
        iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test',
        aud: 'test-client-id',
        exp: Math.floor(Date.now() / 1000) + 3600,
        iat: Math.floor(Date.now() / 1000),
        auth_time: Math.floor(Date.now() / 1000),
        token_use: 'id',
      };
      const base64Payload = btoa(JSON.stringify(payload));
      const token = `header.${base64Payload}.signature`;

      const user = parseIdTokenForUser(token);

      expect(user?.role).toBe(AdminRole.PLATFORM_ADMIN);
      expect(user?.permissions).toContain(Permission.POOL_MANAGE);
    });
  });

  describe('getCurrentUser', () => {
    it('fetches current user from API', async () => {
      const mockResponse = {
        user_id: 'user-123',
        role: AdminRole.ORG_ADMIN,
        org_id: 'org-456',
        dept_id: 'dept-001',
        permissions: [Permission.ORG_READ, Permission.BUDGET_READ],
        created_at: '2024-01-01T00:00:00Z',
      };

      vi.mocked(apiClient.get).mockResolvedValue(mockResponse);

      const result = await getCurrentUser();

      expect(apiClient.get).toHaveBeenCalledWith('/auth/me');
      expect(result?.id).toBe('user-123');
      expect(result?.role).toBe(AdminRole.ORG_ADMIN);
    });

    it('returns null on error', async () => {
      vi.mocked(apiClient.get).mockRejectedValue(new Error('Unauthorized'));

      const result = await getCurrentUser();

      expect(result).toBeNull();
    });
  });

  describe('logout', () => {
    it('clears tokens on logout', async () => {
      // Store some tokens first
      storeTokens({
        access_token: 'test-access',
        id_token: 'test-id',
        refresh_token: 'test-refresh',
        expires_in: 3600,
        token_type: 'Bearer',
      });

      vi.mocked(apiClient.post).mockResolvedValue({});

      await logout();

      expect(getAccessToken()).toBeNull();
      expect(getIdToken()).toBeNull();
    });

    it('clears tokens even if API call fails', async () => {
      storeTokens({
        access_token: 'test-access',
        id_token: 'test-id',
        refresh_token: 'test-refresh',
        expires_in: 3600,
        token_type: 'Bearer',
      });

      vi.mocked(apiClient.post).mockRejectedValue(new Error('Network error'));

      await logout();

      expect(getAccessToken()).toBeNull();
    });
  });

  describe('handleOAuthCallback', () => {
    it('exchanges code for tokens and returns user', async () => {
      // Store PKCE verifier
      storePKCEVerifier('test-verifier');

      const mockTokenResponse: CognitoTokenResponse = {
        access_token: 'new-access-token',
        id_token: createMockIdToken({
          sub: 'user-123',
          email: 'test@example.com',
          'cognito:username': 'testuser',
          'custom:role': 'org_admin',
        }),
        refresh_token: 'new-refresh-token',
        expires_in: 3600,
        token_type: 'Bearer',
      };

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(mockTokenResponse),
      });

      const result = await handleOAuthCallback('auth-code');

      expect(result.token).toBe('new-access-token');
      expect(result.user.id).toBe('user-123');
      expect(result.user.email).toBe('test@example.com');
    });

    it('throws error when PKCE verifier is missing', async () => {
      // Don't store verifier

      await expect(handleOAuthCallback('auth-code')).rejects.toThrow(
        'PKCE verifier not found'
      );
    });
  });

  describe('refreshToken', () => {
    it('uses stored refresh token to get new tokens', async () => {
      // Store initial tokens
      storeTokens({
        access_token: 'old-access',
        id_token: 'old-id',
        refresh_token: 'stored-refresh-token',
        expires_in: 3600,
        token_type: 'Bearer',
      });

      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            access_token: 'new-access-token',
            id_token: 'new-id-token',
            refresh_token: 'stored-refresh-token',
            expires_in: 3600,
            token_type: 'Bearer',
          }),
      });

      const result = await refreshToken();

      expect(result.token).toBe('new-access-token');
      expect(getAccessToken()).toBe('new-access-token');
    });

    it('throws error when no refresh token available', async () => {
      // Don't store any tokens

      await expect(refreshToken()).rejects.toThrow('No refresh token available');
    });
  });
});

// Helper function to create mock ID tokens
function createMockIdToken(payload: Partial<CognitoIdTokenPayload>): string {
  const fullPayload = {
    iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test',
    aud: 'test-client-id',
    exp: Math.floor(Date.now() / 1000) + 3600,
    iat: Math.floor(Date.now() / 1000),
    auth_time: Math.floor(Date.now() / 1000),
    token_use: 'id' as const,
    email_verified: true,
    ...payload,
  };
  const base64Payload = btoa(JSON.stringify(fullPayload));
  return `header.${base64Payload}.signature`;
}
