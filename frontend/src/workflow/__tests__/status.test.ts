import { describe, it, expect } from 'vitest';
import { phaseStatusLabel, statusLabel } from '../status';

describe('statusLabel (Requirement 10, Property 7)', () => {
  it('returns Running when no workflow', () => {
    expect(statusLabel(undefined).label).toBe('Running');
    expect(statusLabel(null).label).toBe('Running');
  });

  it('maps FAILED → Failed (highest precedence)', () => {
    // Failed wins even if at a review step.
    expect(statusLabel({ status: 'FAILED', current_step: 'assessment_review' })).toEqual({
      label: 'Failed',
      type: 'error',
    });
  });

  it('maps CANCELLED/ABORTED → Cancelled', () => {
    expect(statusLabel({ status: 'CANCELLED' }).label).toBe('Cancelled');
    expect(statusLabel({ status: 'ABORTED' }).label).toBe('Cancelled');
  });

  it('maps a review/approval step → Awaiting Approval', () => {
    expect(statusLabel({ status: 'RUNNING', current_step: 'design_review' }).label).toBe('Awaiting Approval');
  });

  it('maps SUCCEEDED/COMPLETED or completed phase → Done', () => {
    expect(statusLabel({ status: 'SUCCEEDED' }).label).toBe('Done');
    expect(statusLabel({ status: 'COMPLETED' }).label).toBe('Done');
    expect(statusLabel({ current_phase: 'completed' }).label).toBe('Done');
  });

  it('defaults to Running', () => {
    expect(statusLabel({ status: 'RUNNING', current_step: 'assessment_running' }).label).toBe('Running');
  });

  it('never emits a disallowed label', () => {
    const allowed = new Set(['Running', 'Awaiting Approval', 'Done', 'Cancelled', 'Failed']);
    for (const raw of ['RUNNING', 'GENERATING', 'UNKNOWN', 'PROVISIONING', '', 'weird']) {
      expect(allowed.has(statusLabel({ status: raw }).label)).toBe(true);
    }
  });

  describe('phaseStatusLabel', () => {
    const atExecReview = { status: 'RUNNING', current_phase: 'execution', current_step: 'execution_review' };
    it('marks earlier phases Done while a later gate is pending', () => {
      for (const p of ['discovery', 'assessment', 'design']) {
        expect(phaseStatusLabel(atExecReview, p).label).toBe('Done');
      }
    });
    it('gives the current phase the workflow status', () => {
      expect(phaseStatusLabel(atExecReview, 'execution').label).toBe('Awaiting Approval');
    });
    it('marks later phases Not Started', () => {
      expect(phaseStatusLabel(atExecReview, 'validation').label).toBe('Not Started');
    });
    it('marks phases skipped by a close-out strategy', () => {
      const atHandover = { status: 'RUNNING', current_phase: 'handover', current_step: 'handover_review' };
      expect(phaseStatusLabel(atHandover, 'design', 'retire').label).toBe('Skipped');
      expect(phaseStatusLabel(atHandover, 'discovery', 'retire').label).toBe('Done');
    });
    it('maps execution sub-phases onto execution', () => {
      expect(phaseStatusLabel({ status: 'RUNNING', current_phase: 'cicd' }, 'design').label).toBe('Done');
    });
  });
});
