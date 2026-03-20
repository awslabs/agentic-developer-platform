/**
 * Authentication Context for Cognito OAuth 2.0 PKCE Flow
 *
 * Manages authentication state including user info, tokens, and session management.
 * Uses Cognito OAuth 2.0 with PKCE for secure authentication.
 */

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import type { User, AuthState, Permission, AdminRole } from '@/types';
import {
  buildLoginUrl,
  refreshToken,
  logout as logoutService,
  getCurrentUserFromToken,
  getAccessToken,
  getTokenExpiry,
  isTokenExpired,
  clearTokens,
} from '@/services/auth';

interface AuthContextType extends AuthState {
  login: () => Promise<void>;
  logout: () => Promise<void>;
  hasPermission: (permission: Permission) => boolean;
  hasRole: (role: AdminRole) => boolean;
  setAuthState: (state: AuthState) => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const navigate = useNavigate();

  // Initialize auth state from stored tokens
  useEffect(() => {
    const initializeAuth = async () => {
      try {
        const storedToken = getAccessToken();

        if (storedToken) {
          // Check if token is expired
          if (isTokenExpired()) {
            // Try to refresh
            try {
              const { token: newToken } = await refreshToken();
              setToken(newToken);
              const currentUser = getCurrentUserFromToken();
              setUser(currentUser);
            } catch {
              // Refresh failed, clear tokens
              clearTokens();
              setToken(null);
              setUser(null);
            }
          } else {
            // Token is valid, restore session
            setToken(storedToken);
            const currentUser = getCurrentUserFromToken();
            setUser(currentUser);
          }
        }
      } catch (err) {
        console.error('Failed to initialize auth:', err);
        clearTokens();
      } finally {
        setIsLoading(false);
      }
    };

    initializeAuth();
  }, []);

  // Token refresh timer
  useEffect(() => {
    if (!token) return;

    const expiry = getTokenExpiry();
    if (!expiry) return;

    const now = Date.now();
    const timeUntilExpiry = expiry - now;

    // Refresh 5 minutes before expiry
    const refreshTime = timeUntilExpiry - 5 * 60 * 1000;

    if (refreshTime <= 0) {
      // Token is about to expire, refresh now
      refreshToken()
        .then(({ token: newToken }) => {
          setToken(newToken);
        })
        .catch(() => {
          // Refresh failed, logout
          logout();
        });
      return;
    }

    const timer = setTimeout(async () => {
      try {
        const { token: newToken } = await refreshToken();
        setToken(newToken);
      } catch {
        // Refresh failed, logout
        await logout();
      }
    }, refreshTime);

    return () => clearTimeout(timer);
  }, [token]);

  /**
   * Initiate login by redirecting to Cognito hosted UI
   */
  const login = useCallback(async () => {
    setIsLoading(true);
    try {
      const loginUrl = await buildLoginUrl();
      window.location.href = loginUrl;
    } catch (err) {
      console.error('Failed to initiate login:', err);
      setIsLoading(false);
      throw err;
    }
  }, []);

  /**
   * Logout user - clear local tokens and optionally redirect to Cognito logout
   */
  const logout = useCallback(async () => {
    try {
      await logoutService();
    } finally {
      clearTokens();
      setToken(null);
      setUser(null);
      navigate('/login');
    }
  }, [navigate]);

  /**
   * Check if user has a specific permission
   */
  const hasPermission = useCallback(
    (permission: Permission): boolean => {
      if (!user) return false;
      return user.permissions.includes(permission);
    },
    [user]
  );

  /**
   * Check if user has a specific role
   */
  const hasRole = useCallback(
    (role: AdminRole): boolean => {
      if (!user) return false;
      return user.role === role;
    },
    [user]
  );

  /**
   * Set auth state directly (used by AuthCallback after OAuth flow)
   */
  const setAuthState = useCallback((state: AuthState) => {
    setUser(state.user);
    setToken(state.token);
  }, []);

  const value: AuthContextType = {
    user,
    token,
    isAuthenticated: !!token && !!user,
    isLoading,
    login,
    logout,
    hasPermission,
    hasRole,
    setAuthState,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuthContext(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuthContext must be used within an AuthProvider');
  }
  return context;
}
