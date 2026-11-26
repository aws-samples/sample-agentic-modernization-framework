import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { isAuthenticated, getAccessToken, login as cognitoLogin, logout as cognitoLogout, handleAuthCallback } from './cognito';
import { isDevAuth, devLogin, getDevUser } from './devAuth';
import { Role, roleFromGroups } from './roles';

interface AuthUser {
  isAuthenticated: boolean;
  roles: string[];
  email?: string;
}

interface AuthContextType {
  user: AuthUser;
  loading: boolean;
  login: () => void | Promise<void>;
  logout: () => void;
  isDevMode: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<AuthUser>({ isAuthenticated: false, roles: [] });
  const [loading, setLoading] = useState(true);
  const devMode = isDevAuth();

  useEffect(() => {
    if (devMode) {
      // Dev mode: fetch token from backend
      devLogin().then(() => {
        const devUser = getDevUser();
        setUser({ isAuthenticated: true, roles: devUser.roles, email: devUser.email });
        setLoading(false);
      });
      return;
    }

    // Production: Cognito authorization code flow with PKCE
    // Check for auth code in URL (redirect back from Cognito)
    handleAuthCallback().then((handled) => {
      if (handled) {
        parseToken();
      } else if (isAuthenticated()) {
        parseToken();
      }
      setLoading(false);
    });
  }, [devMode]);

  const parseToken = () => {
    const token = getAccessToken();
    if (token) {
      try {
        const payload = JSON.parse(atob(token.split('.')[1]));
        setUser({
          isAuthenticated: true,
          roles: payload['cognito:groups'] || [],
          email: payload.email,
        });
      } catch {
        setUser({ isAuthenticated: false, roles: [] });
      }
    }
  };

  const login = () => {
    if (devMode) {
      // Return the promise so callers can await completion and surface
      // failures — swallowing it here leaves the sign-in button unable to
      // report that the API was unreachable.
      return devLogin().then(() => {
        const devUser = getDevUser();
        setUser({ isAuthenticated: true, roles: devUser.roles, email: devUser.email });
      });
    }
    cognitoLogin(); // async — generates PKCE and redirects
  };

  const logout = () => {
    sessionStorage.clear();
    setUser({ isAuthenticated: false, roles: [] });
    if (!devMode) cognitoLogout();
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, isDevMode: devMode }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be inside AuthProvider');
  return ctx;
};

/**
 * The current user's RBAC role (from `cognito:groups` / the dev-auth user's groups).
 * Outside an AuthProvider it fails closed to Viewer. UI-only: the backend enforces.
 */
export const useRole = () => {
  const ctx = useContext(AuthContext);
  const role = roleFromGroups(ctx?.user.roles);
  return { role, hasRole: (minimum: Role) => role >= minimum };
};
