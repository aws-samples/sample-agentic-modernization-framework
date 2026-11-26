import { describe, it, expect } from 'vitest';
import { phaseOf, getPhaseIndex, PHASES } from '../phases';

describe('phaseOf (Phase_Mapping_Source)', () => {
  it('maps canonical phases', () => {
    expect(phaseOf('discovery')).toBe('discovery');
    expect(phaseOf('cutover')).toBe('cutover');
  });
  it('maps aliases to canonical phases', () => {
    expect(phaseOf('migration_plan')).toBe('design');
    expect(phaseOf('planning')).toBe('design');
    expect(phaseOf('transform')).toBe('execution');
    expect(phaseOf('deploy')).toBe('execution'); // Validation follows the deploy
    expect(phaseOf('post_deploy')).toBe('handover');
  });
  it('strips step suffixes', () => {
    expect(phaseOf('assessment_review')).toBe('assessment');
    expect(phaseOf('design_approval')).toBe('design');
    expect(phaseOf('execution_running')).toBe('execution');
  });
  it('returns undefined for unknown', () => {
    expect(phaseOf('nonsense')).toBeUndefined();
    expect(phaseOf(undefined)).toBeUndefined();
  });
});

describe('getPhaseIndex', () => {
  it('indexes core phases in order', () => {
    expect(getPhaseIndex('discovery')).toBe(0);
    expect(getPhaseIndex('handover')).toBe(PHASES.length - 1);
  });
  it('treats completed as past-all', () => {
    expect(getPhaseIndex('completed')).toBe(PHASES.length);
  });
  it('returns -1 for unknown', () => {
    expect(getPhaseIndex('nonsense')).toBe(-1);
  });
});
