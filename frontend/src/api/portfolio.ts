import { apiClient } from './client';

export const listApps = () => apiClient.get('/portfolio/apps');
export const getApp = (appId: string) => apiClient.get(`/portfolio/apps/${appId}`);
export const updateApp = (appId: string, data: any) => apiClient.put(`/portfolio/apps/${appId}`, data);
