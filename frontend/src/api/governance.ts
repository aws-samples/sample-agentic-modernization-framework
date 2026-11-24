import { apiClient } from './client';

/** Estate-wide governance rollup: runs, gates, apps under governance. */
export const getGovernanceSummary = () => apiClient.get('/governance/summary');

/** One app's governance detail: executions, gates, artifacts. */
export const getGovernance = (appId: string) =>
  apiClient.get(`/governance/${appId}`);
