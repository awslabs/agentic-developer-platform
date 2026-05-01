import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  buildGitHubLoginUrl,
  buildLoginUrl,
  parseIdTokenForUser,
} from '@/services/auth';
import { AdminRole } from '@/types';

// Mock the cognito config module
vi.mock('@/config/cognito', () => ({
  getCognitoConfig: () => ({
    userPoolId: 'us-east-1_TestPool',
    clientId: 'test-client-id',
    domain: 'test-domain',
    region: 'us-east-1',
    redirectUri: 'http://localhost:3000/auth/callback',
  }),
  getCognitoAuthorizeUrl: () =>
    'https://test-domain.auth.us-east-1.amazoncognito.com/oauth2/authorize',
  getCognitoTokenUrl: () =>
    'https://test-domain.auth.us-east-1.amazoncognito.com/oauth2/token',
  getCognitoLogoutUrl: () =>
    'https://test-domain.auth.us-east-1.amazoncognito.com/logout',
}));

// Mock crypto.subtle for PKCE generation
const mockDigest = vi.fn().mockResolvedValue(new ArrayBuffer(32));
Object.defineProperty(globalThis, 'crypto', {
  value: {
    getRandomValues: (arr: Uint8Array) => {
      for (let i = 0; i < arr.length; i++) arr[i] = i % 256;
      return arr;
    },
    subtle: { digest: mockDigest },
  },
});

describe('buildGitHubLoginUrl', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('includes identity_provider=GitHub parameter', async () => {
    const url = await buildGitHubLoginUrl();
    const params = new URL(url).searchParams;

    expect(params.get('identity_provider')).toBe('GitHub');
    expect(params.get('response_type')).toBe('code');
    expect(params.get('client_id')).toBe('test-client-id');
    expect(params.get('scope')).toBe('openid email profile');
    expect(params.get('code_challenge_method')).toBe('S256');
    expect(params.get('code_challenge')).toBeTruthy();
  });

  it('stores PKCE verifier in sessionStorage', async () => {
    await buildGitHubLoginUrl();
    expect(sessionStorage.getItem('pkce_code_verifier')).toBeTruthy();
  });
});

describe('buildLoginUrl', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('does NOT include identity_provider parameter', async () => {
    const url = await buildLoginUrl();
    const params = new URL(url).searchParams;

    expect(params.get('identity_provider')).toBeNull();
    expect(params.get('response_type')).toBe('code');
    expect(params.get('client_id')).toBe('test-client-id');
  });
});

describe('parseIdTokenForUser - GitHub identity', () => {
  function makeToken(payload: Record<string, unknown>): string {
    const header = btoa(JSON.stringify({ alg: 'RS256' }));
    const body = btoa(JSON.stringify(payload));
    return `${header}.${body}.fake-signature`;
  }

  it('extracts GitHub login from identities claim', () => {
    const token = makeToken({
      sub: 'user-123',
      email: 'dev@example.com',
      email_verified: true,
      'cognito:username': 'GitHub_12345',
      identities: JSON.stringify([
        { providerName: 'GitHub', userId: 'octocat' },
      ]),
      picture: 'https://avatars.githubusercontent.com/u/12345',
      iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool',
      aud: 'test-client-id',
      exp: Math.floor(Date.now() / 1000) + 3600,
      iat: Math.floor(Date.now() / 1000),
      auth_time: Math.floor(Date.now() / 1000),
      token_use: 'id',
    });

    const user = parseIdTokenForUser(token);

    expect(user).not.toBeNull();
    expect(user!.githubLogin).toBe('octocat');
    expect(user!.avatarUrl).toBe(
      'https://avatars.githubusercontent.com/u/12345'
    );
  });

  it('constructs avatar URL from GitHub login when picture is absent', () => {
    const token = makeToken({
      sub: 'user-456',
      email: 'dev@example.com',
      email_verified: true,
      'cognito:username': 'GitHub_67890',
      identities: JSON.stringify([
        { providerName: 'GitHub', userId: 'monalisa' },
      ]),
      iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool',
      aud: 'test-client-id',
      exp: Math.floor(Date.now() / 1000) + 3600,
      iat: Math.floor(Date.now() / 1000),
      auth_time: Math.floor(Date.now() / 1000),
      token_use: 'id',
    });

    const user = parseIdTokenForUser(token);

    expect(user!.githubLogin).toBe('monalisa');
    expect(user!.avatarUrl).toBe(
      'https://avatars.githubusercontent.com/monalisa'
    );
  });

  it('uses GitHub login as display name when name is absent', () => {
    const token = makeToken({
      sub: 'user-789',
      email: 'dev@example.com',
      email_verified: true,
      'cognito:username': 'GitHub_111',
      identities: JSON.stringify([
        { providerName: 'GitHub', userId: 'devuser' },
      ]),
      iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool',
      aud: 'test-client-id',
      exp: Math.floor(Date.now() / 1000) + 3600,
      iat: Math.floor(Date.now() / 1000),
      auth_time: Math.floor(Date.now() / 1000),
      token_use: 'id',
    });

    const user = parseIdTokenForUser(token);

    expect(user!.name).toBe('devuser');
  });

  it('handles native Cognito user without GitHub identity', () => {
    const token = makeToken({
      sub: 'user-native',
      email: 'native@example.com',
      email_verified: true,
      name: 'Native User',
      'cognito:username': 'native@example.com',
      'custom:role': 'platform_admin',
      iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool',
      aud: 'test-client-id',
      exp: Math.floor(Date.now() / 1000) + 3600,
      iat: Math.floor(Date.now() / 1000),
      auth_time: Math.floor(Date.now() / 1000),
      token_use: 'id',
    });

    const user = parseIdTokenForUser(token);

    expect(user!.githubLogin).toBeUndefined();
    expect(user!.avatarUrl).toBeUndefined();
    expect(user!.name).toBe('Native User');
    expect(user!.role).toBe(AdminRole.PLATFORM_ADMIN);
  });
});
