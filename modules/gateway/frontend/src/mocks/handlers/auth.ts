import { http, HttpResponse } from 'msw';
import { currentUser } from '../data/users';

// Cognito OAuth token endpoint handler for all Cognito domains
const cognitoTokenHandler = http.post(
  /https:\/\/.*\.auth\..*\.amazoncognito\.com\/oauth2\/token/,
  async ({ request }) => {
    const body = await request.text();
    const params = new URLSearchParams(body);
    const grantType = params.get('grant_type');
    const code = params.get('code');
    const refreshToken = params.get('refresh_token');

    // Simulate error for invalid codes
    if (grantType === 'authorization_code' && code === 'invalid-code') {
      return HttpResponse.json(
        { error: 'invalid_grant', error_description: 'Invalid code' },
        { status: 400 }
      );
    }

    // Simulate error for expired refresh token
    if (grantType === 'refresh_token' && refreshToken === 'expired-refresh-token') {
      return HttpResponse.json(
        { error: 'invalid_grant', error_description: 'Refresh token expired' },
        { status: 400 }
      );
    }

    // Create a mock ID token payload
    const mockIdTokenPayload = {
      sub: 'user-123',
      email: 'test@example.com',
      'cognito:username': 'testuser',
      'custom:org_id': 'org-456',
      'custom:role': 'org_admin',
      iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test',
      aud: 'test-client-id',
      exp: Math.floor(Date.now() / 1000) + 3600,
      iat: Math.floor(Date.now() / 1000),
      auth_time: Math.floor(Date.now() / 1000),
      token_use: 'id',
    };
    const mockIdToken = `header.${btoa(JSON.stringify(mockIdTokenPayload))}.signature`;

    return HttpResponse.json({
      access_token: grantType === 'refresh_token' ? 'new-access-token' : 'mock-access-token',
      id_token: mockIdToken,
      refresh_token: refreshToken || 'mock-refresh-token',
      expires_in: 3600,
      token_type: 'Bearer',
    });
  }
);

export const authHandlers = [
  cognitoTokenHandler,

  // Exchange credentials
  http.post('/api/auth/exchange', async ({ request }) => {
    const body = await request.json() as {
      aws_access_key_id: string;
      aws_secret_access_key: string;
      aws_session_token: string;
    };

    // Validate credentials exist
    if (!body.aws_access_key_id || !body.aws_secret_access_key || !body.aws_session_token) {
      return HttpResponse.json(
        { error: 'Invalid credentials', message: 'Missing required credential fields' },
        { status: 400 }
      );
    }

    return HttpResponse.json({
      token: 'mock-jwt-token-' + Date.now(),
      expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
      user_id: currentUser.user_id,
      org_id: currentUser.org_id || '',
      team_id: '',
      department_id: currentUser.dept_id || '',
      account_type: 'human',
    });
  }),

  // Refresh token
  http.post('/api/auth/refresh', () => {
    return HttpResponse.json({
      token: 'mock-jwt-token-refreshed-' + Date.now(),
      expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
    });
  }),

  // Logout
  http.post('/api/auth/logout', () => {
    return HttpResponse.json({ success: true });
  }),

  // Get current user
  http.get('/api/auth/me', () => {
    return HttpResponse.json(currentUser);
  }),
];
