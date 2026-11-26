import { API_BASE, apiClient } from './client';

export const getAnalysisByType = (appId: string, analysisType: string) =>
  apiClient.get(`/analysis/${appId}/${analysisType}`);

/** Aggregated "missing ATX TD" gaps across all apps' assessments. */
export const getTransformationGaps = () =>
  apiClient.get('/analysis/transformation-gaps');

/** (Re)compute the deterministic migration risk score (0-100, higher = worse). */
export const computeRiskScore = (appId: string) =>
  apiClient.post(`/analysis/${appId}/risk-score`);

/** Read the latest persisted risk-score analysis record (if any). */
export const getRiskScore = (appId: string) =>
  apiClient.get(`/analysis/${appId}/risk-score`);

/** Returns the URL for the HTML report iframe */
export const getAnalysisHtmlUrl = (appId: string, analysisType: string) =>
  `${API_BASE}/analysis/${appId}/${analysisType}/html`;
