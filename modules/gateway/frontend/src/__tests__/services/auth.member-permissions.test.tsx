/**
 * Issue #4389 — member → USAGE_READ must be reachable in parseIdTokenForUser.
 *
 * #4019 added `ROLE_PERMISSIONS[AdminRole.MEMBER] = [USAGE_READ]` to auth.ts but
 * not the `custom:role` branch that selects it, so `role` stayed `undefined` for
 * every member and `permissions` resolved to `[]`. Any USAGE_READ-gated surface
 * (including the #4324 budget/spend dashboard) rendered blank for legitimate
 * members. These tests fail on main prior to the fix.
 *
 * The security direction matters as much as the fix: a member must resolve
 * EXACTLY [USAGE_READ], and an ABSENT claim must still resolve to no role and no
 * permissions — defaulting an unapproved user to MEMBER would be the
 * over-correction (silent grant) this issue's impact table warns about.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { apiClient } from '@/services/api';
import {
  parseIdTokenForUser,
  getCurrentUser,
  getCurrentUserFromToken,
  storeTokens,
} from '@/services/auth';
import { AuthProvider, useAuthContext } from '@/contexts/AuthContext';
import { AdminRole, Permission } from '@/types';
import type { CognitoIdTokenPayload } from '@/types';

vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

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

/**
 * Build an unsigned ID token whose payload carries the given claims. Signature
 * is irrelevant — parseIdTokenForUser decodes without verifying (the backend
 * verifies against JWKS).
 */
function makeIdToken(claims: Partial<CognitoIdTokenPayload>): string {
  const now = Math.floor(Date.now() / 1000);
  const payload: Partial<CognitoIdTokenPayload> = {
    sub: 'member-user-1',
    email: 'member@example.com',
    name: 'Regular Member',
    'cognito:username': 'member',
    iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test',
    aud: 'test-client-id',
    exp: now + 3600,
    iat: now,
    auth_time: now,
    token_use: 'id',
    ...claims,
  };
  return `header.${btoa(JSON.stringify(payload))}.signature`;
}

describe('Issue #4389 — member permissions on the ID-token path', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  describe('member synonyms resolve MEMBER + USAGE_READ', () => {
    // The backend treats all three as AdminRole.MEMBER
    // (_MEMBERSHIP_ROLE_TO_ADMIN_ROLE, src/admin/config.py). The frontend must
    // agree on the whole synonym set, not just the canonical string.
    const memberSynonyms = ['member', 'user', 'viewer'];

    it.each(memberSynonyms)('custom:role "%s" grants USAGE_READ', (customRole) => {
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': customRole }));

      expect(user).not.toBeNull();
      expect(user?.permissions).toContain(Permission.USAGE_READ);
    });

    it.each(memberSynonyms)('custom:role "%s" resolves AdminRole.MEMBER', (customRole) => {
      // Role must be set (not just permissions) so the role badge renders.
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': customRole }));

      expect(user?.role).toBe(AdminRole.MEMBER);
    });

    it('preserves the rest of the member identity alongside the permission', () => {
      const user = parseIdTokenForUser(
        makeIdToken({
          'custom:role': 'member',
          'custom:org_id': 'org-456',
          'custom:department_id': 'dept-789',
        })
      );

      expect(user?.id).toBe('member-user-1');
      expect(user?.email).toBe('member@example.com');
      expect(user?.orgId).toBe('org-456');
      expect(user?.deptId).toBe('dept-789');
    });
  });

  describe('no over-correction — a member gains nothing beyond USAGE_READ', () => {
    it('resolves EXACTLY [USAGE_READ]', () => {
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': 'member' }));

      expect(user?.permissions).toEqual([Permission.USAGE_READ]);
    });

    it.each([
      Permission.BUDGET_READ,
      Permission.BUDGET_UPDATE,
      Permission.USER_MANAGE,
      Permission.USER_READ,
      Permission.ORG_READ,
      Permission.POOL_MANAGE,
      Permission.LOGS_READ,
      Permission.PLAN_APPROVE,
    ])('does not grant %s', (permission) => {
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': 'member' }));

      expect(user?.permissions).not.toContain(permission);
    });
  });

  describe('absent or unknown claim grants nothing', () => {
    it('absent custom:role leaves role undefined and permissions empty', () => {
      // Deliberate: an unapproved/unassigned user (e.g. a fresh GitHub signup)
      // must not be silently upgraded to MEMBER. The UI hides the role badge.
      const user = parseIdTokenForUser(makeIdToken({}));

      expect(user).not.toBeNull();
      expect(user?.role).toBeUndefined();
      expect(user?.permissions).toEqual([]);
    });

    it('unrecognized custom:role yields empty permissions without throwing', () => {
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': 'bogus_role' }));

      expect(user?.role).toBeUndefined();
      expect(user?.permissions).toEqual([]);
    });

    it('always returns an array, never undefined, so hasPermission cannot throw', () => {
      // AuthContext.hasPermission does `user.permissions.includes(...)` unguarded.
      for (const customRole of [undefined, 'bogus_role', 'member', 'org_admin']) {
        const user = parseIdTokenForUser(
          makeIdToken(customRole ? { 'custom:role': customRole } : {})
        );

        expect(Array.isArray(user?.permissions)).toBe(true);
        expect(() => user?.permissions.includes(Permission.USAGE_READ)).not.toThrow();
      }
    });
  });

  describe('admin roles are unchanged (regression)', () => {
    it.each([
      ['platform_admin', AdminRole.PLATFORM_ADMIN],
      ['org_admin', AdminRole.ORG_ADMIN],
      ['dept_admin', AdminRole.DEPT_ADMIN],
    ] as const)('custom:role "%s" still resolves %s', (customRole, expectedRole) => {
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': customRole }));

      expect(user?.role).toBe(expectedRole);
      // Admins keep strictly more than the member's single permission.
      expect(user?.permissions).toContain(Permission.USAGE_READ);
      expect(user?.permissions.length).toBeGreaterThan(1);
    });

    it('admin roles are not downgraded to the member permission set', () => {
      const user = parseIdTokenForUser(makeIdToken({ 'custom:role': 'org_admin' }));

      expect(user?.permissions).toContain(Permission.USER_MANAGE);
    });
  });

  describe('getCurrentUser — the latent second instance', () => {
    it('yields an empty array when /auth/me returns no role and no permissions', async () => {
      // GET /auth/me (src/auth/routes.py) returns neither field. The old
      // `response.permissions || ROLE_PERMISSIONS[response.role]` produced
      // `undefined`, which would make AuthContext.hasPermission throw.
      vi.mocked(apiClient.get).mockResolvedValue({
        user_id: 'member-user-1',
        org_id: 'org-456',
        department_id: 'dept-789',
        account_type: 'user',
        is_admin: false,
        expires_at: '2026-01-01T00:00:00Z',
      });

      const user = await getCurrentUser();

      expect(user).not.toBeNull();
      expect(user?.permissions).toEqual([]);
      expect(user?.permissions).not.toBeUndefined();
      expect(() => user?.permissions.includes(Permission.USAGE_READ)).not.toThrow();
    });

    it('derives permissions from role when /auth/me returns a role but no permissions', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        user_id: 'member-user-1',
        role: AdminRole.MEMBER,
        created_at: '2026-01-01T00:00:00Z',
      });

      const user = await getCurrentUser();

      expect(user?.permissions).toEqual([Permission.USAGE_READ]);
    });

    it('prefers explicit permissions from the response when present', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        user_id: 'user-123',
        role: AdminRole.ORG_ADMIN,
        permissions: [Permission.ORG_READ],
        created_at: '2026-01-01T00:00:00Z',
      });

      const user = await getCurrentUser();

      expect(user?.permissions).toEqual([Permission.ORG_READ]);
    });
  });

  describe('end-to-end: a member session restored from an ID token', () => {
    // Deliberately does NOT mock @/services/auth — this drives the real
    // parseIdTokenForUser through AuthContext, which is the path every session
    // restore takes (AuthContext -> getCurrentUserFromToken -> parseIdTokenForUser).
    // This is the assertion that a USAGE_READ-gated surface renders for a member.
    function wrapper({ children }: { children: ReactNode }) {
      return (
        <BrowserRouter>
          <AuthProvider>{children}</AuthProvider>
        </BrowserRouter>
      );
    }

    it('grants hasPermission(USAGE_READ) to a member', async () => {
      storeTokens({
        access_token: makeIdToken({ 'custom:role': 'member' }),
        id_token: makeIdToken({ 'custom:role': 'member' }),
        refresh_token: 'refresh',
        expires_in: 3600,
        token_type: 'Bearer',
      });

      const { result } = renderHook(() => useAuthContext(), { wrapper });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.user?.role).toBe(AdminRole.MEMBER);
      // The gate the #4324 dashboard and every usage view depend on.
      expect(result.current.hasPermission(Permission.USAGE_READ)).toBe(true);
      // Still no admin authority.
      expect(result.current.hasPermission(Permission.USER_MANAGE)).toBe(false);
      expect(result.current.hasPermission(Permission.BUDGET_UPDATE)).toBe(false);
    });

    it('grants nothing to a session whose token carries no role claim', async () => {
      storeTokens({
        access_token: makeIdToken({}),
        id_token: makeIdToken({}),
        refresh_token: 'refresh',
        expires_in: 3600,
        token_type: 'Bearer',
      });

      const { result } = renderHook(() => useAuthContext(), { wrapper });

      await waitFor(() => {
        expect(result.current.isLoading).toBe(false);
      });

      expect(result.current.user?.role).toBeUndefined();
      expect(result.current.hasPermission(Permission.USAGE_READ)).toBe(false);
    });
  });
});

/**
 * Sanity: the ID-token path is the one the live SPA actually uses, so the
 * service-level helper must agree with parseIdTokenForUser for a member.
 */
describe('Issue #4389 — getCurrentUserFromToken reads the member role', () => {
  it('resolves USAGE_READ from the stored ID token', () => {
    storeTokens({
      access_token: 'access',
      id_token: makeIdToken({ 'custom:role': 'member' }),
      refresh_token: 'refresh',
      expires_in: 3600,
      token_type: 'Bearer',
    });

    const user = getCurrentUserFromToken();

    expect(user?.role).toBe(AdminRole.MEMBER);
    expect(user?.permissions).toEqual([Permission.USAGE_READ]);
  });
});
