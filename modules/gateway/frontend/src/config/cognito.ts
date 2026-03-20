/**
 * Cognito OAuth 2.0 Configuration
 *
 * This module provides configuration for Cognito OAuth 2.0 PKCE authentication flow.
 * Values are read from Vite environment variables at build time.
 */

import type { CognitoConfig } from '@/types';

/**
 * Get Cognito configuration from environment variables.
 * These values are injected at build time via Vite.
 */
export function getCognitoConfig(): CognitoConfig {
  const userPoolId = import.meta.env.VITE_COGNITO_USER_POOL_ID;
  const clientId = import.meta.env.VITE_COGNITO_CLIENT_ID;
  const domain = import.meta.env.VITE_COGNITO_DOMAIN;
  const region = import.meta.env.VITE_COGNITO_REGION || 'us-east-1';
  const redirectUri = import.meta.env.VITE_REDIRECT_URI || `${window.location.origin}/auth/callback`;

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

  return {
    userPoolId: userPoolId || 'us-east-1_5rYm3yrrY', // Fallback to known dev pool
    clientId: clientId || '', // Must be set in production
    domain: domain || 'bedrockgw-dev-auth',
    region,
    redirectUri,
  };
}

/**
 * Build the Cognito hosted UI base URL
 */
export function getCognitoHostedUiUrl(): string {
  const config = getCognitoConfig();
  return `https://${config.domain}.auth.${config.region}.amazoncognito.com`;
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
