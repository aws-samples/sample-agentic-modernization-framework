import { apiClient } from './client';

export interface PendingApproval {
  step: string;
  message: string;
  received_at: string;
  /** Deterministic gate summary (invoke_runtime._build_decision_summary). */
  decision_summary?: {
    warnings?: string[];
    contradictions?: string[];
    /** Execution gate: IaC / CI/CD generation notes (placeholder handler, CI-only pipeline, …). */
    notes?: string[];
    /** Assessment gate: CI-readiness gaps the Execution fixer will address. */
    auto_fixable_at_execution?: string[];
    /** Assessment gate: runtime wiring detected by the repo scan. */
    detected_wiring?: Record<string, unknown>;
    /** Deploy gate: the migration-branch commit an approval deploys. */
    head_commit?: string;
    review_branch?: string;
    review_url?: string;
    [key: string]: unknown;
  };
}

export interface ActiveWorkflow {
  app_id: string;
  status: string;
  execution_arn?: string;
  current_phase: string;
  current_step: string;
  started_at: string;
  waiting_since?: string;
  pending: PendingApproval[];
}

export const startWorkflow = (appId: string) =>
  apiClient.post('/workflow/start', { app_id: appId });

export const approveStep = (appId: string, step: string, approved: boolean, comment = '', inputs?: Record<string, unknown>) =>
  apiClient.post('/workflow/approve', { app_id: appId, step, approved, comment, ...(inputs ? { inputs } : {}) });

export const getActiveWorkflows = (): Promise<ActiveWorkflow[]> =>
  apiClient.get('/workflow/active');

export const cancelWorkflow = (appId: string, reason = 'Cancelled by user') =>
  apiClient.post('/workflow/cancel', { app_id: appId, reason });

export interface ToolCall {
  tool: string;
  target: string;
  caller?: string;       // 'agent' (via MCP Gateway) | 'workflow' (Step Functions); '' on old traces
  phase: string;
  status: string;        // 'ok' | 'error'
  latency_ms: number;
  at: string;
  input: any;
  output: any;
}

/** Why the app's latest run failed — reconstructed from the Step Functions history. */
export interface FailureReason {
  failed: boolean;
  status: string;
  execution_arn: string;
  kind: 'task_error' | 'precondition' | 'post_check' | 'rejected' | 'aborted' | 'timeout' | 'unknown';
  failed_state: string;
  phase: string;
  summary: string;
  error: string;
  cause: string;
  reasons: string[];
  warnings: string[];
  at: string;
}

export const getFailureReason = (appId: string): Promise<FailureReason> =>
  apiClient.get(`/workflow/failure/${appId}`);

/** Captured MCP tool-call traces for an app, optionally filtered to one phase. */
export const getToolCalls = (appId: string, phase = ''): Promise<ToolCall[]> =>
  apiClient.get(`/workflow/tool-calls/${appId}`, { params: phase ? { phase } : {} });

export interface ExecutionJob {
  job_id: string;
  task_id: string;
  task_arn: string;
  status: string;        // PENDING | RUNNING | COMPLETED | FAILED
  mode: string;          // workbench | code-analysis
  td_name: string;
  created_at: string;
  log_group: string;
  log_stream: string;
}

export interface ExecutionStep {
  step_number: number;
  td_name: string;
  repo_name: string;
  repo_branch: string;
  status: string;        // pending | running | completed | failed
  message: string;
  updated_at: string;
  s3_report: string;     // presigned URL to this step's validation report (if any)
}

export interface ExecutionArtifact {
  key: string;
  name: string;
  run_id: string;
  size: number;
  url: string;           // presigned GET URL
}

export interface ExecutionTransform {
  plan_id: string;
  plan_status: string;
  s3_prefix: string;
  task_id: string;
  task_arn: string;
  status: string;
  created_at: string;
  log_group: string;
  log_stream: string;
  artifacts: ExecutionArtifact[];
}

export interface ExecutionReportMeta {
  type: 'execution-database' | 'execution-infra' | 'execution-cicd';
  present: boolean;
  status: string;
  timestamp: string;
  run_id: string;
  content?: string;   // inline markdown (infra/CI-CD built from EXECUTION# records)
}

export interface ExecutionCommit {
  kind: string;       // 'IaC' | 'CI/CD' | 'Transformation'
  repo: string;
  branch: string;
  commit_id: string;  // empty for the transformation branch link
  url: string;        // CodeCommit console link
  created_at?: string;
}

export interface ExecutionDetails {
  app_id: string;
  execution_arn?: string;
  current_phase: string;
  current_step: string;
  /** Deterministic execution findings (e.g. CI/CD provider will not run on the repo host). */
  warnings?: string[];
  sub_status: { database: string; infra: string; cicd: string };
  jobs: ExecutionJob[];
  steps: ExecutionStep[];
  plan_id: string;
  plan_status: string;
  transform: ExecutionTransform;
  analysis_run_count: number;
  reports: ExecutionReportMeta[];
  commits: ExecutionCommit[];
}

/** Consolidated execution-phase view (ECS jobs, per-step progress, reports). */
export const getExecutionDetails = (appId: string): Promise<ExecutionDetails> =>
  apiClient.get(`/workflow/execution/${appId}`);
