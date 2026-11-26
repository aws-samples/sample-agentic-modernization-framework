// Shared workflow types — single source of truth for the review screens.
// Kept framework-agnostic and pure so they can be unit-tested in isolation.

/** The canonical migration phases (core journey). Cutover is conditional. */
export type Phase =
  | 'discovery'
  | 'assessment'
  | 'design'
  | 'execution'
  | 'validation'
  | 'cutover'
  | 'handover';

/** The only status labels allowed to be displayed anywhere. */
export type WorkflowStatusLabel =
  | 'Running'
  | 'Awaiting Approval'
  | 'Done'
  | 'Cancelled'
  | 'Failed'
  // Per-phase only (phaseStatusLabel): a phase the workflow has not reached yet,
  // or one a close-out strategy (retire/retain/repurchase) skips entirely.
  | 'Not Started'
  | 'Skipped';

/** Cloudscape StatusIndicator "type" values we map onto. */
export type StatusIndicatorType = 'in-progress' | 'success' | 'stopped' | 'error' | 'pending';

export interface WorkflowStatus {
  label: WorkflowStatusLabel;
  type: StatusIndicatorType;
}

/** Minimal shape of the active-workflow record used by the UI. */
export interface WorkflowRecord {
  status?: string; // raw backend status (e.g. RUNNING, COMPLETED, FAILED, CANCELLED)
  current_phase?: string;
  current_step?: string;
  waiting_since?: string;
  execution_arn?: string;
  started_at?: string;
}
