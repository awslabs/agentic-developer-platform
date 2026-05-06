/**
 * Authentication Service for Cognito OAuth 2.0 PKCE Flow
 *
 * This module implements OAuth 2.0 Authorization Code flow with PKCE
 * (Proof Key for Code Exchange) for secure authentication with AWS Cognito.
 */

import { apiClient } from './api';
import { AdminRole, Permission } from '@/types';
import type {
  CognitoTokenResponse,
  PKCEChallenge,
  CognitoIdTokenPayload,
  User,
} from '@/types';
import {
  getCognitoConfig,
  getCognitoAuthorizeUrl,
  getCognitoTokenUrl,
  getCognitoLogoutUrl,
} from '@/config/cognito';

// Storage keys for auth tokens and PKCE
const ACCESS_TOKEN_KEY = 'cognito_access_token';
const ID_TOKEN_KEY = 'cognito_id_token';
const REFRESH_TOKEN_KEY = 'cognito_refresh_token';
const TOKEN_EXPIRY_KEY = 'cognito_token_expiry';
const PKCE_VERIFIER_KEY = 'pkce_code_verifier';

// Role to permissions mapping (matching backend)
const ROLE_PERMISSIONS: Record<AdminRole, Permission[]> = {
  [AdminRole.PLATFORM_ADMIN]: [
    Permission.ORG_CREATE,
    Permission.ORG_READ,
    Permission.ORG_UPDATE,
    Permission.ORG_DELETE,
    Permission.BUDGET_READ,
    Permission.BUDGET_UPDATE,
    Permission.RATELIMIT_READ,
    Permission.RATELIMIT_UPDATE,
    Permission.POOL_READ,
    Permission.POOL_MANAGE,
    Permission.USAGE_READ,
    Permission.LOGS_READ,
    Permission.LOGS_EXPORT,
    Permission.USER_READ,
    Permission.USER_MANAGE,
    Permission.METRICS_READ,
  ],
  [AdminRole.ORG_ADMIN]: [
    Permission.ORG_READ,
    Permission.ORG_UPDATE,
    Permission.BUDGET_READ,
    Permission.BUDGET_UPDATE,
    Permission.RATELIMIT_READ,
    Permission.RATELIMIT_UPDATE,
    Permission.USAGE_READ,
    Permission.LOGS_READ,
    Permission.LOGS_EXPORT,
    Permission.USER_READ,
    Permission.USER_MANAGE,
  ],
  [AdminRole.DEPT_ADMIN]: [
    Permission.BUDGET_READ,
    Permission.RATELIMIT_READ,
    Permission.USAGE_READ,
    Permission.LOGS_READ,
    Permission.USER_READ,
  ],
};

// ============================================================================
// PKCE Utilities
// ============================================================================

/**
 * Generate a cryptographically random string for PKCE code verifier
 */
function generateRandomString(length: number): string {
  const charset = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~';
  const randomValues = new Uint8Array(length);
  crypto.getRandomValues(randomValues);
  return Array.from(randomValues)
    .map((v) => charset[v % charset.length])
    .join('');
}

/**
 * Generate SHA-256 hash of the code verifier for PKCE challenge
 */
async function sha256(plain: string): Promise<ArrayBuffer> {
  const encoder = new TextEncoder();
  const data = encoder.encode(plain);
  return crypto.subtle.digest('SHA-256', data);
}

/**
 * Base64 URL encode (without padding) for PKCE challenge
 */
function base64UrlEncode(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  bytes.forEach((b) => {
    binary += String.fromCharCode(b);
  });
  return btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

/**
 * Generate PKCE code verifier and challenge pair
 */
export async function generatePKCEChallenge(): Promise<PKCEChallenge> {
  // Generate a random 43-128 character string for the verifier
  const verifier = generateRandomString(64);
  // Create SHA-256 hash and base64url encode for the challenge
  const hashed = await sha256(verifier);
  const challenge = base64UrlEncode(hashed);

  return { verifier, challenge };
}

/**
 * Store PKCE verifier in sessionStorage for the callback
 */
export function storePKCEVerifier(verifier: string): void {
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
}

/**
 * Retrieve and clear PKCE verifier from sessionStorage
 */
export function getPKCEVerifier(): string | null {
  const verifier = sessionStorage.getItem(PKCE_VERIFIER_KEY);
  sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  return verifier;
}

// ============================================================================
// OAuth Login/Logout URLs
// ============================================================================

/**
 * Build the Cognito hosted UI login URL with PKCE challenge
 */
export async function buildLoginUrl(): Promise<string> {
  const config = getCognitoConfig();
  const pkce = await generatePKCEChallenge();

  // Store verifier for the callback
  storePKCEVerifier(pkce.verifier);

  // Build the authorization URL with PKCE
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    scope: 'openid email profile',
    code_challenge: pkce.challenge,
    code_challenge_method: 'S256',
  });

  return `${getCognitoAuthorizeUrl()}?${params.toString()}`;
}

/**
 * Build the GitHub sign-in URL via the Lambda auth broker (Issue #520).
 *
 * Instead of routing through Cognito hosted UI (which requires OIDC discovery
 * that GitHub doesn't support), this redirects to our broker Lambda's /start
 * endpoint. The broker handles the GitHub OAuth flow and returns Cognito tokens.
 *
 * The broker URL is configured via VITE_GITHUB_AUTH_BROKER_URL env var.
 */
export async function buildGitHubLoginUrl(): Promise<string> {
  const brokerUrl = import.meta.env.VITE_GITHUB_AUTH_BROKER_URL;
  if (!brokerUrl) {
    throw new Error('GitHub sign-in is not configured (VITE_GITHUB_AUTH_BROKER_URL not set)');
  }
  // The broker's /start endpoint handles state generation and redirects to GitHub
  return `${brokerUrl.replace(/\/$/, '')}/start`;
}

/**
 * Build the Cognito logout URL
 */
export function buildLogoutUrl(): string {
  const config = getCognitoConfig();

  const params = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: window.location.origin,
  });

  return `${getCognitoLogoutUrl()}?${params.toString()}`;
}

// ============================================================================
// Token Exchange and Refresh
// ============================================================================

/**
 * Exchange authorization code for tokens using PKCE verifier
 */
export async function exchangeCodeForTokens(
  code: string,
  verifier: string
): Promise<CognitoTokenResponse> {
  const config = getCognitoConfig();

  const params = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: config.clientId,
    code,
    redirect_uri: config.redirectUri,
    code_verifier: verifier,
  });

  const response = await fetch(getCognitoTokenUrl(), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: params.toString(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error_description || 'Token exchange failed');
  }

  return response.json();
}

/**
 * Refresh access token using refresh token
 */
export async function refreshAccessToken(refreshToken: string): Promise<CognitoTokenResponse> {
  const config = getCognitoConfig();

  const params = new URLSearchParams({
    grant_type: 'refresh_token',
    client_id: config.clientId,
    refresh_token: refreshToken,
  });

  const response = await fetch(getCognitoTokenUrl(), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: params.toString(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error_description || 'Token refresh failed');
  }

  return response.json();
}

// ============================================================================
// Token Storage
// ============================================================================

/**
 * Store tokens in sessionStorage
 */
export function storeTokens(tokens: CognitoTokenResponse): void {
  sessionStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
  sessionStorage.setItem(ID_TOKEN_KEY, tokens.id_token);
  if (tokens.refresh_token) {
    sessionStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
  }

  // Calculate and store expiry time
  const expiryTime = Date.now() + tokens.expires_in * 1000;
  sessionStorage.setItem(TOKEN_EXPIRY_KEY, expiryTime.toString());
}

/**
 * Get stored access token
 */
export function getAccessToken(): string | null {
  return sessionStorage.getItem(ACCESS_TOKEN_KEY);
}

/**
 * Get stored ID token
 */
export function getIdToken(): string | null {
  return sessionStorage.getItem(ID_TOKEN_KEY);
}

/**
 * Get stored refresh token
 */
export function getRefreshToken(): string | null {
  return sessionStorage.getItem(REFRESH_TOKEN_KEY);
}

/**
 * Get token expiry timestamp
 */
export function getTokenExpiry(): number | null {
  const expiry = sessionStorage.getItem(TOKEN_EXPIRY_KEY);
  return expiry ? parseInt(expiry, 10) : null;
}

/**
 * Clear all stored tokens
 */
export function clearTokens(): void {
  sessionStorage.removeItem(ACCESS_TOKEN_KEY);
  sessionStorage.removeItem(ID_TOKEN_KEY);
  sessionStorage.removeItem(REFRESH_TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_EXPIRY_KEY);
  sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  // Also clear legacy auth tokens if present
  sessionStorage.removeItem('auth_token');
  sessionStorage.removeItem('auth_expires');
}

// ============================================================================
// Token Parsing and Validation
// ============================================================================

/**
 * Parse JWT token payload without verification (client-side only)
 * For actual verification, the backend validates against JWKS
 */
export function parseTokenPayload<T>(token: string): T | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;

    const payload = JSON.parse(atob(parts[1]));
    return payload as T;
  } catch {
    return null;
  }
}

/**
 * Check if access token is expired or about to expire
 */
export function isTokenExpired(bufferMinutes: number = 5): boolean {
  const expiry = getTokenExpiry();
  if (!expiry) return true;

  const bufferMs = bufferMinutes * 60 * 1000;
  return Date.now() >= expiry - bufferMs;
}

/**
 * Parse ID token to extract user information.
 * Handles both native Cognito users and GitHub-federated users.
 */
export function parseIdTokenForUser(idToken: string): User | null {
  const payload = parseTokenPayload<CognitoIdTokenPayload>(idToken);
  if (!payload) return null;

  // Determine role from custom attribute or default to ORG_ADMIN
  let role: AdminRole;
  const customRole = payload['custom:role'];
  if (customRole === 'platform_admin') {
    role = AdminRole.PLATFORM_ADMIN;
  } else if (customRole === 'dept_admin') {
    role = AdminRole.DEPT_ADMIN;
  } else {
    role = AdminRole.ORG_ADMIN;
  }

  // Extract GitHub identity info if present
  let avatarUrl: string | undefined;
  let githubLogin: string | undefined;

  if (payload.identities) {
    try {
      const identities = JSON.parse(payload.identities);
      const githubIdentity = identities.find(
        (id: { providerName: string }) => id.providerName === 'GitHub'
      );
      if (githubIdentity) {
        githubLogin = githubIdentity.userId;
      }
    } catch {
      // Ignore parse errors for identities
    }
  }

  // Use picture claim (Cognito maps GitHub avatar_url to picture)
  if (payload.picture) {
    avatarUrl = payload.picture;
  } else if (githubLogin) {
    // Fallback: construct avatar URL from GitHub login
    avatarUrl = `https://avatars.githubusercontent.com/${githubLogin}`;
  }

  // Use GitHub login as display name fallback
  const displayName = payload.name || githubLogin;

  return {
    id: payload.sub,
    email: payload.email,
    name: displayName,
    role,
    orgId: payload['custom:org_id'],
    deptId: payload['custom:department_id'],
    permissions: ROLE_PERMISSIONS[role],
    createdAt: new Date(payload.auth_time * 1000).toISOString(),
    avatarUrl,
    githubLogin,
  };
}

// ============================================================================
// User Session Management
// ============================================================================

/**
 * Get the current authenticated user from stored tokens
 */
export function getCurrentUserFromToken(): User | null {
  const idToken = getIdToken();
  if (!idToken) return null;

  return parseIdTokenForUser(idToken);
}

/**
 * Get the current user from the backend /auth/me endpoint
 */
export async function getCurrentUser(): Promise<User | null> {
  try {
    const response = await apiClient.get<{
      user_id: string;
      role: AdminRole;
      org_id?: string;
      dept_id?: string;
      permissions: Permission[];
      created_at: string;
      email?: string;
      name?: string;
    }>('/auth/me');

    return {
      id: response.user_id,
      role: response.role,
      orgId: response.org_id,
      deptId: response.dept_id,
      permissions: response.permissions || ROLE_PERMISSIONS[response.role],
      createdAt: response.created_at,
      email: response.email,
      name: response.name,
    };
  } catch {
    return null;
  }
}

/**
 * Perform logout - clear local tokens and redirect to Cognito logout
 */
export async function logout(): Promise<void> {
  try {
    // Optionally notify backend of logout
    await apiClient.post('/auth/logout').catch(() => {
      // Ignore logout errors
    });
  } finally {
    // Clear all local tokens
    clearTokens();
  }
}

/**
 * Perform full logout with redirect to Cognito logout page
 */
export function logoutWithRedirect(): void {
  // Clear local tokens first
  clearTokens();
  // Redirect to Cognito logout
  window.location.href = buildLogoutUrl();
}

// ============================================================================
// Legacy API Support (for gradual migration)
// ============================================================================

export interface LoginResponse {
  user: User;
  token: string;
  expiresAt: string;
}

/**
 * Handle the OAuth callback - exchange code for tokens and return user info
 */
export async function handleOAuthCallback(code: string): Promise<LoginResponse> {
  const verifier = getPKCEVerifier();
  if (!verifier) {
    throw new Error('PKCE verifier not found - please restart login');
  }

  // Exchange authorization code for tokens
  const tokens = await exchangeCodeForTokens(code, verifier);

  // Store tokens
  storeTokens(tokens);

  // Parse user from ID token
  const user = parseIdTokenForUser(tokens.id_token);
  if (!user) {
    throw new Error('Failed to parse user from ID token');
  }

  return {
    user,
    token: tokens.access_token,
    expiresAt: new Date(Date.now() + tokens.expires_in * 1000).toISOString(),
  };
}

/**
 * Refresh the current session using stored refresh token
 */
export async function refreshToken(): Promise<{ token: string; expiresAt: string }> {
  const storedRefreshToken = getRefreshToken();
  if (!storedRefreshToken) {
    throw new Error('No refresh token available');
  }

  const tokens = await refreshAccessToken(storedRefreshToken);
  storeTokens(tokens);

  return {
    token: tokens.access_token,
    expiresAt: new Date(Date.now() + tokens.expires_in * 1000).toISOString(),
  };
}
