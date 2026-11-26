// Portfolio / program-level API (Requirements 65–68).
// Backed by backend/src/api/v1/portfolio_program.py.
import { apiClient } from './client';

export interface ProgramApp {
  app_id: string;
  app_name: string;
  business_unit: string;
  strategy: string;
  wave: number | string | null;
  phase: string;
  step: string;
  status: string; // Running | Awaiting Approval | Done | Cancelled | Failed
  completed_at?: string;
}

export interface WaveBucket {
  wave: number | string | null;
  label: string;
  total: number;
  Running: number;
  'Awaiting Approval': number;
  Done: number;
  Cancelled: number;
  Failed: number;
  completion_pct: number;
}

export interface AtRiskApp {
  app_id: string;
  app_name: string;
  phase: string;
  status: string;
  wave: number | string | null;
  reasons: string[];
}

export interface ProgramMetrics {
  total: number;
  completed: number;
  completion_pct: number;
  status_counts: Record<string, number>;
  velocity_per_week: number;
  burndown: Array<{ weeks_ago: number; remaining: number }>;
  insufficient_data: boolean;
}

export const getDashboard = () =>
  apiClient.get<any, { applications: ProgramApp[]; count: number }>('/portfolio/dashboard');

export const getWaves = () =>
  apiClient.get<any, { waves: WaveBucket[] }>('/portfolio/waves');

export const getAtRisk = (agingHours = 48) =>
  apiClient.get<any, { at_risk: AtRiskApp[]; count: number; aging_hours: number }>(
    `/portfolio/at-risk?aging_hours=${agingHours}`,
  );

export const getMetrics = (weeks = 8) =>
  apiClient.get<any, ProgramMetrics>(`/portfolio/metrics?weeks=${weeks}`);
