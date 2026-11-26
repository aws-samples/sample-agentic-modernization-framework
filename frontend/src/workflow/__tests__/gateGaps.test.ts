import { describe, it, expect } from 'vitest';
import { completenessGaps, gapPayload, retryComment } from '../gateGaps';

describe('completenessGaps', () => {
  it('parses the design gate warning (APP-CLI)', () => {
    expect(completenessGaps(['completeness gate (design) missing: compliance, data_classification']))
      .toEqual(['compliance', 'data_classification']);
  });
  it('ignores non-fillable fields and other warnings', () => {
    expect(completenessGaps(['Missing design.md section(s): Testing.', 'completeness gate (assessment) missing: owners, rpo']))
      .toEqual(['rpo']);
    expect(completenessGaps(undefined)).toEqual([]);
  });
});

describe('gapPayload / retryComment', () => {
  it('keeps compliance as a list and drops empties', () => {
    const p = gapPayload({ compliance: ['None', ' '], data_classification: 'internal', rpo: '  ' });
    expect(p).toEqual({ compliance: ['None'], data_classification: 'internal' });
  });
  it('appends the provided inputs to the reviewer comment', () => {
    const c = retryComment('Fix the health check path.', { compliance: ['HIPAA'], data_classification: 'restricted' });
    expect(c).toBe('Fix the health check path. Previously missing inputs are now on the app record — use them: '
      + 'compliance=HIPAA; data_classification=restricted.');
    expect(retryComment('', {})).toBe('');
  });
});
