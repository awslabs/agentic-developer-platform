/**
 * Cognito domain handling — prefix vs custom domain.
 *
 * VITE_COGNITO_DOMAIN carries whichever form the deployment uses, because it is
 * published from `aws_cognito_user_pool_domain.main.domain` and that attribute is
 * the prefix for a Cognito-hosted domain and the FQDN for a custom one. Getting
 * this wrong is expensive: the value is baked into the bundle at build time, so a
 * malformed hosted-UI URL ships to every user and needs another rebuild to undo.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const load = async (domain: string, region = 'us-east-1') => {
  vi.stubEnv('VITE_COGNITO_DOMAIN', domain);
  vi.stubEnv('VITE_COGNITO_REGION', region);
  vi.stubEnv('VITE_COGNITO_USER_POOL_ID', 'us-east-1_TEST');
  vi.stubEnv('VITE_COGNITO_CLIENT_ID', 'testclientid');
  vi.resetModules();
  return import('@/config/cognito');
};

describe('Cognito hosted UI URL', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => vi.unstubAllEnvs());

  it('appends the regional suffix to a prefix domain', async () => {
    const { getCognitoHostedUiUrl } = await load('bedrockgw-dev-auth');
    expect(getCognitoHostedUiUrl()).toBe(
      'https://bedrockgw-dev-auth.auth.us-east-1.amazoncognito.com'
    );
  });

  it('uses a custom domain FQDN as-is', async () => {
    const { getCognitoHostedUiUrl } = await load('auth.example.com');
    expect(getCognitoHostedUiUrl()).toBe('https://auth.example.com');
  });

  it('does not append the suffix to a custom domain — the bug this guards', async () => {
    const { getCognitoHostedUiUrl } = await load('auth.example.com');
    expect(getCognitoHostedUiUrl()).not.toContain('amazoncognito.com');
  });

  it('derives authorize, token and logout from the same base', async () => {
    const m = await load('auth.example.com');
    expect(m.getCognitoAuthorizeUrl()).toBe('https://auth.example.com/oauth2/authorize');
    expect(m.getCognitoTokenUrl()).toBe('https://auth.example.com/oauth2/token');
    expect(m.getCognitoLogoutUrl()).toBe('https://auth.example.com/logout');
  });

  it('keeps JWKS and issuer on cognito-idp regardless of domain form', async () => {
    // The issuer is always cognito-idp.<region>.amazonaws.com/<pool-id> — a
    // custom domain does not change token validation.
    const m = await load('auth.example.com');
    expect(m.getCognitoJwksUrl()).toContain('cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST');
    expect(m.getCognitoIssuerUrl()).toContain('cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST');
  });
});
