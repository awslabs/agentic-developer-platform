/**
 * Cognito OAuth 2.0 Configuration
 *
 * This module provides configuration for Cognito OAuth 2.0 PKCE authentication flow.
 * Values are read from Vite environment variables at build time.
 */

import { deploymentSetting } from '@/config/runtime';

import type { CognitoConfig } from '@/types';

/**
 * Get Cognito configuration from environment variables.
 * Release values come from runtime-config.js; local builds use Vite settings.
 */
export function getCognitoConfig(): CognitoConfig {
  const userPoolId = deploymentSetting('VITE_COGNITO_USER_POOL_ID');
  const clientId = deploymentSetting('VITE_COGNITO_CLIENT_ID');
  const domain = deploymentSetting('VITE_COGNITO_DOMAIN');
  const region = deploymentSetting('VITE_COGNITO_REGION') || 'us-east-1';
  const redirectUri = deploymentSetting('VITE_REDIRECT_URI') || `${window.location.origin}/auth/callback`;

  // Validate required configuration
  if (!userPoolId) {
    console.warn('VITE_COGNITO_USER_POOL_ID not set - using fallback');
  }
  if (!clientId) {
    console.warn('VITE_COGNITO_CLIENT_ID not set - using fallback');
  }
  if (!domain) {
    console.warn('VITE_COGNITO_DOMAIN not set - using fallback');
  }

  // No fallback values — a silent-wrong-pool is harder to debug than a loud
  // "not configured". isCognitoConfigured() below returns false if any of the
  // three required fields are empty, which the UI surfaces to the user.
  return {
    userPoolId: userPoolId || '',
    clientId: clientId || '',
    domain: domain || '',
    region,
    redirectUri,
  };
}

/**
 * Build the Cognito hosted UI base URL
 */
export function getCognitoHostedUiUrl(): string {
  const config = getCognitoConfig();
  // VITE_COGNITO_DOMAIN carries either form of Cognito domain:
  //   - a prefix, e.g. "bedrockgw-dev-auth", which needs the regional suffix
  //   - a custom domain FQDN, e.g. "auth.example.com", which is already complete
  // A prefix domain is a single DNS label — alphanumeric and hyphens only — so a
  // dot distinguishes the two unambiguously rather than by guesswork. Appending
  // the suffix to an FQDN produces a hostname that does not resolve, and because
  // this value is baked into the bundle at build time the failure ships to every
  // user and needs another rebuild to undo.
  const isCustomDomain = config.domain.includes('.');
  return isCustomDomain
    ? `https://${config.domain}`
    : `https://${config.domain}.auth.${config.region}.amazoncognito.com`;
}

/**
 * Build the Cognito token endpoint URL
 */
export function getCognitoTokenUrl(): string {
  return `${getCognitoHostedUiUrl()}/oauth2/token`;
}

/**
 * Build the Cognito authorization endpoint URL
 */
export function getCognitoAuthorizeUrl(): string {
  return `${getCognitoHostedUiUrl()}/oauth2/authorize`;
}

/**
 * Build the Cognito logout endpoint URL
 */
export function getCognitoLogoutUrl(): string {
  return `${getCognitoHostedUiUrl()}/logout`;
}

/**
 * Build the JWKS URL for token validation
 */
export function getCognitoJwksUrl(): string {
  const config = getCognitoConfig();
  return `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}/.well-known/jwks.json`;
}

/**
 * Build the issuer URL for token validation
 */
export function getCognitoIssuerUrl(): string {
  const config = getCognitoConfig();
  return `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`;
}

/**
 * Check if Cognito is properly configured
 */
export function isCognitoConfigured(): boolean {
  const config = getCognitoConfig();
  return !!(config.userPoolId && config.clientId && config.domain);
}

export default getCognitoConfig;
