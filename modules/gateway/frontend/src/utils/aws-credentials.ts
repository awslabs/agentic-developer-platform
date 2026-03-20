/**
 * AWS credential utilities for browser-based STS credential exchange.
 *
 * @deprecated This module is deprecated in favor of Cognito OAuth 2.0 PKCE authentication.
 * These utilities remain available for backward compatibility with service account
 * authentication flows but should not be used for new user authentication.
 *
 * For user authentication, use the auth service methods in @/services/auth:
 * - buildLoginUrl() - Redirects to Cognito hosted UI
 * - handleOAuthCallback() - Exchanges authorization code for tokens
 * - refreshToken() - Refreshes the access token
 */

export interface AWSCredentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
}

export interface STSCredentials {
  AccessKeyId: string;
  SecretAccessKey: string;
  SessionToken: string;
  Expiration: string;
}

/**
 * Validates AWS credential format
 */
export function validateCredentials(credentials: AWSCredentials): {
  valid: boolean;
  errors: string[];
} {
  const errors: string[] = [];

  if (!credentials.accessKeyId) {
    errors.push('Access Key ID is required');
  } else if (!/^[A-Z0-9]{16,}$/i.test(credentials.accessKeyId)) {
    errors.push('Access Key ID format is invalid');
  }

  if (!credentials.secretAccessKey) {
    errors.push('Secret Access Key is required');
  } else if (credentials.secretAccessKey.length < 20) {
    errors.push('Secret Access Key is too short');
  }

  if (!credentials.sessionToken) {
    errors.push('Session Token is required');
  } else if (credentials.sessionToken.length < 100) {
    errors.push('Session Token appears to be invalid');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}

/**
 * Parses credentials from AWS CLI output format
 */
export function parseAWSCLIOutput(output: string): AWSCredentials | null {
  try {
    // Try JSON format first
    const json = JSON.parse(output);
    if (json.Credentials) {
      return {
        accessKeyId: json.Credentials.AccessKeyId,
        secretAccessKey: json.Credentials.SecretAccessKey,
        sessionToken: json.Credentials.SessionToken,
      };
    }
    // Direct credential object
    if (json.AccessKeyId) {
      return {
        accessKeyId: json.AccessKeyId,
        secretAccessKey: json.SecretAccessKey,
        sessionToken: json.SessionToken,
      };
    }
    return null;
  } catch {
    // Try to parse as environment variables
    const accessKeyMatch = output.match(/AWS_ACCESS_KEY_ID[=\s]+([A-Z0-9]+)/i);
    const secretKeyMatch = output.match(/AWS_SECRET_ACCESS_KEY[=\s]+([^\s]+)/i);
    const tokenMatch = output.match(/AWS_SESSION_TOKEN[=\s]+([^\s]+)/i);

    if (accessKeyMatch && secretKeyMatch && tokenMatch) {
      return {
        accessKeyId: accessKeyMatch[1],
        secretAccessKey: secretKeyMatch[1],
        sessionToken: tokenMatch[1],
      };
    }

    return null;
  }
}

/**
 * Formats credentials for display (masked)
 */
export function formatCredentialsMasked(credentials: AWSCredentials): {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
} {
  return {
    accessKeyId: credentials.accessKeyId
      ? `${credentials.accessKeyId.substring(0, 4)}****${credentials.accessKeyId.substring(credentials.accessKeyId.length - 4)}`
      : '',
    secretAccessKey: credentials.secretAccessKey ? '********' : '',
    sessionToken: credentials.sessionToken
      ? `${credentials.sessionToken.substring(0, 10)}...`
      : '',
  };
}

/**
 * Gets credentials expiration time
 */
export function getCredentialExpiration(expirationStr: string): {
  expiresAt: Date;
  isExpired: boolean;
  expiresIn: number;
} {
  const expiresAt = new Date(expirationStr);
  const now = new Date();
  const expiresIn = expiresAt.getTime() - now.getTime();

  return {
    expiresAt,
    isExpired: expiresIn <= 0,
    expiresIn: Math.max(0, expiresIn),
  };
}

/**
 * Formats expiration time for display
 */
export function formatExpirationTime(expiresIn: number): string {
  if (expiresIn <= 0) {
    return 'Expired';
  }

  const seconds = Math.floor(expiresIn / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}
