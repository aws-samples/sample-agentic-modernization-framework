/**
 * Dev-only auth bypass.
 * When VITE_DEV_AUTH=true, fetches a dev token from the backend.
 * The backend is the single source of truth for dev user identity.
 */

import { apiClient } from '../api/client';

const DEV_AUTH = import.meta.env.VITE_DEV_AUTH === 'true';

export const isDevAuth = (): boolean => DEV_AUTH;

interface DevLoginResponse {
  access_token: string;
  id_token: string;
  user: {
    email: string;
    username: string;
    groups: string[];
  };
}

let _devUser: { email: string; username: string; roles: string[] } | null = null;

/**
 * Login by calling the backend dev-login endpoint.
 * Caches the token in sessionStorage — doesn't re-fetch on every refresh.
 */
export const devLogin = async (): Promise<void> => {
  // If we already have a valid token in session, skip the API call
  const existing = sessionStorage.getItem('access_token');
  if (existing && existing.length > 10) {
    // Restore dev user from token
    try {
      const payload = JSON.parse(atob(existing.split('.')[1]));
      // The fallback token below is unsigned with iss 'dev-local' — but so is
      // the legitimate backend dev token, so alg/iss cannot tell them apart.
      // Only the sub claim distinguishes them. Without this check, one failed
      // dev-login pins the app to the fallback token for its full 24h exp,
      // stranding the user on 401s even after the backend recovers.
      const isFallback = payload.sub === 'dev-user-fallback';
      if (!isFallback && payload.exp && payload.exp > Math.floor(Date.now() / 1000)) {
        _devUser = {
          email: payload.email || 'dev@migration-framework.local',
          username: payload['cognito:username'] || 'dev-admin',
          roles: payload['cognito:groups'] || ['admins', 'migration-leads', 'migration-engineers', 'viewers'],
        };
        return;
      }
      if (isFallback) {
        // Drop the poisoned token so the real endpoint is retried below.
        sessionStorage.removeItem('access_token');
        sessionStorage.removeItem('id_token');
      }
    } catch {
      // Token parse failed — re-fetch
    }
  }

  try {
    const resp = await apiClient.get<DevLoginResponse>('/auth/dev-login');
    const data = (resp as any)?.access_token ? resp as any : (resp as any)?.data ?? resp;
    sessionStorage.setItem('access_token', data.access_token);
    sessionStorage.setItem('id_token', data.id_token);
    _devUser = {
      email: data.user.email,
      username: data.user.username,
      roles: data.user.groups,
    };
  } catch {
    // Fallback: create a minimal unsigned token so the UI can render.
    const header = btoa(JSON.stringify({ alg: 'none', typ: 'JWT' }));
    const payload = btoa(JSON.stringify({
      sub: 'dev-user-fallback',
      'cognito:username': 'dev-admin',
      email: 'dev@migration-framework.local',
      'cognito:groups': ['admins', 'migration-leads', 'migration-engineers', 'viewers'],
      iat: Math.floor(Date.now() / 1000),
      exp: Math.floor(Date.now() / 1000) + 86400,
      iss: 'dev-local',
      token_use: 'access',
    }));
    const token = `${header}.${payload}.`;
    sessionStorage.setItem('access_token', token);
    sessionStorage.setItem('id_token', token);
    _devUser = {
      email: 'dev@migration-framework.local',
      username: 'dev-admin',
      roles: ['admins', 'migration-leads', 'migration-engineers', 'viewers'],
    };
  }
};

export const getDevUser = () => _devUser ?? {
  email: 'dev@migration-framework.local',
  username: 'dev-admin',
  roles: ['admins', 'migration-leads', 'migration-engineers', 'viewers'],
};
