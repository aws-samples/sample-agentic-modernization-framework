// Status_Label_Source (Requirement 10, Property 7).
// The SINGLE authority that maps a workflow record to one of the five allowed
// status labels. No screen may display any other status text.

import { getPhaseIndex } from './phases';
import type { WorkflowRecord, WorkflowStatus } from './types';

const CANCELLED = new Set(['CANCELLED', 'ABORTED', 'CANCELED']);
const DONE = new Set(['SUCCEEDED', 'COMPLETED', 'DONE']);

/**
 * Resolve the displayed status for a workflow.
 *
 * Precedence (Property 7): Failed → Cancelled → Awaiting Approval → Done → Running.
 * A phase is "awaiting approval" when its current step is a review/approval gate.
 */
export function statusLabel(wf?: WorkflowRecord | null): WorkflowStatus {
  if (!wf) return { label: 'Running', type: 'in-progress' };

  const raw = (wf.status || '').toUpperCase();
  const step = (wf.current_step || '').toLowerCase();
  const phase = (wf.current_phase || '').toLowerCase();

  if (raw === 'FAILED') return { label: 'Failed', type: 'error' };
  if (CANCELLED.has(raw)) return { label: 'Cancelled', type: 'stopped' };

  // At a human approval gate.
  if (step.includes('review') || step.includes('approval') || step.endsWith('_approve')) {
    return { label: 'Awaiting Approval', type: 'stopped' };
  }

  if (DONE.has(raw) || phase === 'completed') return { label: 'Done', type: 'success' };

  // Default: work is in progress.
  return { label: 'Running', type: 'in-progress' };
}

// 7R close-out strategies jump Discovery → Handover (ASL Handover_CloseOut).
const CLOSE_OUT = new Set(['retire', 'retain', 'repurchase']);
const CLOSE_OUT_SKIPPED = new Set(['assessment', 'design', 'execution', 'validation', 'cutover']);

/**
 * Status of ONE phase relative to where the workflow is now. `statusLabel` is the
 * workflow's status and only describes the current phase — earlier phases are
 * Done and later ones Not Started, even while the workflow waits at a later gate.
 */
export function phaseStatusLabel(
  wf: WorkflowRecord | null | undefined,
  phase: string,
  strategy?: string | null,
): WorkflowStatus {
  const target = getPhaseIndex(phase);
  const current = getPhaseIndex(wf?.current_phase);
  if (!wf || target < 0 || current < 0) return statusLabel(wf);

  if (CLOSE_OUT.has((strategy || '').toLowerCase()) && CLOSE_OUT_SKIPPED.has(phase)) {
    return { label: 'Skipped', type: 'stopped' };
  }
  if (target < current) return { label: 'Done', type: 'success' };
  if (target > current) {
    const raw = (wf.status || '').toUpperCase();
    return raw === 'FAILED' || CANCELLED.has(raw)
      ? { label: 'Cancelled', type: 'stopped' }
      : { label: 'Not Started', type: 'pending' };
  }
  return statusLabel(wf);
}
