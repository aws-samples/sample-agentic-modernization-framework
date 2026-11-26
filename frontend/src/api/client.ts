import axios from 'axios';
import { getAccessToken } from '../auth/cognito';
import { DEMO_READ_ONLY, DEMO_READ_ONLY_MESSAGE } from '../demo';

export const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api/v1';
const DEV_AUTH = import.meta.env.VITE_DEV_AUTH === 'true';

export class ApiClientError extends Error {
  code: string;
  status?: number;
  details?: Record<string, unknown>;

  constructor(code: string, message: string, status?: number, details?: Record<string, unknown>) {
    super(message);
    this.name = 'ApiClientError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export const apiErrorMessage = (error: unknown): string => {
  if (error instanceof Error && error.message) return error.message;
  return 'AMF data is temporarily unavailable. Retry after checking the API readiness endpoint.';
};

const looksLikeHtml = (value: unknown): boolean =>
  typeof value === 'string' && /^\s*(?:<!doctype|<html)/i.test(value);

const envelopeError = (body: any, status?: number): ApiClientError => {
  const payload = body?.error || body?.data || {};
  const code = payload?.code || 'API_REQUEST_FAILED';
  const message = payload?.message || body?.message || 'AMF API request failed.';
  return new ApiClientError(code, message, status, payload?.details);
};

/**
 * FastAPI `HTTPException` bodies are `{"detail": "..."}` (422s: `{"detail": [{loc, msg}, ...]}`).
 * Return a readable message, or null when the body has no usable detail.
 */
export const detailMessage = (body: any): string | null => {
  const detail = body?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail) && detail.length) {
    return detail.map((d: any) => {
      if (typeof d === 'string') return d;
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== 'body').join('.') : '';
      const msg = d?.msg || d?.message || JSON.stringify(d);
      return loc ? `${loc}: ${msg}` : msg;
    }).join('; ');
  }
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
  return null;
};

/** Role named by the backend's 403 detail (`Requires ADMIN role. Your role: VIEWER`), if any. */
const requiredRoleLabel = (serverMessage: string | null): string => {
  const m = serverMessage?.match(/requires\s+(viewer|engineer|admin)\b/i);
  if (!m) return 'Engineer/Admin';
  const r = m[1].toLowerCase();
  return r.charAt(0).toUpperCase() + r.slice(1);
};

export const permissionDeniedMessage = (serverMessage: string | null): string =>
  `You don't have permission for this action (requires ${requiredRoleLabel(serverMessage)}).`;

export const apiClient = axios.create({ baseURL: API_BASE });

apiClient.interceptors.request.use((config) => {
  const method = (config.method || 'get').toUpperCase();
  if (DEMO_READ_ONLY && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    return Promise.reject(new ApiClientError('DEMO_READ_ONLY', DEMO_READ_ONLY_MESSAGE, 403));
  }
  const token = getAccessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

apiClient.interceptors.response.use(
  (response) => {
    const body = response.data;
    if (looksLikeHtml(body)) {
      return Promise.reject(new ApiClientError(
        'API_RESPONSE_NOT_JSON',
        'The AMF API returned the web application instead of JSON. Check CloudFront API routing and access controls.',
        response.status,
      ));
    }
    if (body && body.status === 'error') {
      return Promise.reject(envelopeError(body, response.status));
    }
    return body?.data ?? body;
  },
  (error) => {
    if (error instanceof ApiClientError) {
      return Promise.reject(error);
    }
    const status = error.response?.status as number | undefined;
    const body = error.response?.data;

    if (status === 401 && !DEV_AUTH) {
      sessionStorage.clear();
      window.location.href = '/login';
    }

    if (looksLikeHtml(body)) {
      return Promise.reject(new ApiClientError(
        'API_RESPONSE_NOT_JSON',
        'The AMF API returned HTML instead of JSON. Check CloudFront API routing and access controls.',
        status,
      ));
    }
    if (status === 403) {
      const serverMessage = detailMessage(body) || body?.error?.message || body?.message || null;
      return Promise.reject(new ApiClientError(
        'FORBIDDEN',
        permissionDeniedMessage(serverMessage),
        status,
        serverMessage ? { server_message: serverMessage } : undefined,
      ));
    }
    if (body?.status === 'error' || body?.error || body?.data?.message) {
      return Promise.reject(envelopeError(body, status));
    }
    if (status === 401 && DEV_AUTH) {
      return Promise.reject(new ApiClientError(
        'DEV_SESSION_EXPIRED',
        'The local DEV MODE session expired. Retry sign-in after confirming /health/ready is green.',
        status,
      ));
    }
    const detail = detailMessage(body);
    if (detail) {
      // e.g. PUT /portfolio/apps/{id} rejecting a privileged field (400) — show the server's reason.
      return Promise.reject(new ApiClientError(
        status === 422 ? 'VALIDATION_ERROR' : 'API_REQUEST_FAILED', detail, status,
      ));
    }
    return Promise.reject(new ApiClientError(
      'API_UNREACHABLE',
      error.message || 'Unable to reach the AMF API.',
      status,
    ));
  },
);
