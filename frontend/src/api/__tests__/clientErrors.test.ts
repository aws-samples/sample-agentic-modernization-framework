import { afterEach, describe, expect, it, vi } from 'vitest';
import { AxiosError, type AxiosAdapter, type AxiosResponse } from 'axios';

// Not read-only demo mode: mutations reach the (fake) adapter.
vi.mock('../../auth/cognito', () => ({ getAccessToken: () => null }));
vi.mock('../../demo', () => ({ DEMO_READ_ONLY: false, DEMO_READ_ONLY_MESSAGE: '' }));

import { apiClient, detailMessage, permissionDeniedMessage } from '../client';

const originalAdapter = apiClient.defaults.adapter;
afterEach(() => { apiClient.defaults.adapter = originalAdapter; });

/** Adapter that fails with `status` + `data`, the way axios does for a non-2xx response. */
const failing = (status: number, data: unknown): AxiosAdapter => async (config) => {
  const response = { data, status, statusText: '', headers: {}, config } as AxiosResponse;
  throw new AxiosError(`Request failed with status code ${status}`, 'ERR_BAD_RESPONSE', config, null, response);
};

describe('403 → permission message', () => {
  it('names the role from the backend detail', async () => {
    apiClient.defaults.adapter = failing(403, { detail: 'Requires ADMIN role. Your role: ENGINEER' });
    await expect(apiClient.post('/workflow/approve', {})).rejects.toMatchObject({
      code: 'FORBIDDEN',
      status: 403,
      message: "You don't have permission for this action (requires Admin).",
      details: { server_message: 'Requires ADMIN role. Your role: ENGINEER' },
    });
  });

  it('falls back to Engineer/Admin when the detail does not name a role', async () => {
    apiClient.defaults.adapter = failing(403, {});
    await expect(apiClient.put('/portfolio/apps/APP-1', {})).rejects.toMatchObject({
      code: 'FORBIDDEN',
      message: "You don't have permission for this action (requires Engineer/Admin).",
    });
    expect(permissionDeniedMessage(null)).toContain('requires Engineer/Admin');
  });

  it('keeps an HTML 403 (WAF/CloudFront) as a routing error, not an RBAC one', async () => {
    apiClient.defaults.adapter = failing(403, '<html><body>Forbidden</body></html>');
    await expect(apiClient.get('/portfolio/apps')).rejects.toMatchObject({ code: 'API_RESPONSE_NOT_JSON' });
  });
});

describe('FastAPI detail is surfaced, not swallowed', () => {
  it('shows a 400 privileged-field rejection from PUT /portfolio/apps/{id}', async () => {
    apiClient.defaults.adapter = failing(400, { detail: "Field 'expose_publicly' can only be set by an Admin" });
    await expect(apiClient.put('/portfolio/apps/APP-1', { expose_publicly: true })).rejects.toMatchObject({
      status: 400,
      message: "Field 'expose_publicly' can only be set by an Admin",
    });
  });

  it('formats 422 validation arrays', () => {
    expect(detailMessage({ detail: [{ loc: ['body', 'vpc_cidr'], msg: 'invalid CIDR' }, { loc: ['query', 'x'], msg: 'required' }] }))
      .toBe('vpc_cidr: invalid CIDR; query.x: required');
    expect(detailMessage({ detail: { message: 'nope' } })).toBe('nope');
    expect(detailMessage({})).toBeNull();
  });
});
