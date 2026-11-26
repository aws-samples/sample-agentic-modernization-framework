import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, AxiosResponse } from 'axios';

vi.mock('../../auth/cognito', () => ({ getAccessToken: () => null }));
vi.mock('../../demo', () => ({
  DEMO_READ_ONLY: true,
  DEMO_READ_ONLY_MESSAGE: 'Read-only demo mode is enabled.',
}));

import { ApiClientError, apiClient, apiErrorMessage } from '../client';

const originalAdapter = apiClient.defaults.adapter;

const adapterReturning = (response: Partial<AxiosResponse>): AxiosAdapter => async (config) => ({
  data: null,
  status: 200,
  statusText: 'OK',
  headers: {},
  config,
  ...response,
} as AxiosResponse);

afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
});

describe('apiClient reliability contract', () => {
  it('rejects an HTML SPA fallback instead of treating it as API data', async () => {
    apiClient.defaults.adapter = adapterReturning({
      data: '<!doctype html><html><body>AMF</body></html>',
      headers: { 'content-type': 'text/html' } as any,
    });

    await expect(apiClient.get('/portfolio/apps')).rejects.toMatchObject({
      code: 'API_RESPONSE_NOT_JSON',
    });
  });

  it('preserves structured API error code and message', async () => {
    apiClient.defaults.adapter = adapterReturning({
      data: {
        status: 'error',
        error: {
          code: 'AWS_CREDENTIALS_EXPIRED',
          message: 'AWS credentials expired. Refresh the configured AWS session and retry.',
        },
      },
      status: 503,
      statusText: 'Service Unavailable',
    });

    await expect(apiClient.get('/portfolio/apps')).rejects.toMatchObject({
      code: 'AWS_CREDENTIALS_EXPIRED',
      status: 503,
      message: 'AWS credentials expired. Refresh the configured AWS session and retry.',
    });
  });

  it('renders a useful fallback for unknown errors', () => {
    expect(apiErrorMessage(new ApiClientError('API_UNREACHABLE', 'Unable to reach AMF.')))
      .toBe('Unable to reach AMF.');
    expect(apiErrorMessage(null)).toContain('temporarily unavailable');
  });
});


describe('read-only demo guard', () => {
  it('blocks mutations before the network adapter is called', async () => {
    const adapter = vi.fn(adapterReturning({ data: { ok: true } }));
    apiClient.defaults.adapter = adapter;

    await expect(apiClient.post('/workflow/start', { app_id: 'APP-CATALOG' }))
      .rejects.toMatchObject({ code: 'DEMO_READ_ONLY', status: 403 });
    expect(adapter).not.toHaveBeenCalled();
  });
});
