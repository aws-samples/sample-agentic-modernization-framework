import { describe, expect, it } from 'vitest';

import { checkLabel, consoleLinks, diffRuns, indicatorOf, summarize, tierOf, verdictIndicator } from '../validationView';

describe('validation view model', () => {
  it('groups checks into tiers by prefix', () => {
    expect(tierOf('alb_ingress_scope')).toBe('L0 Platform');
    expect(tierOf('L1:health')).toBe('L1 Smoke');
    expect(tierOf('L2:create product')).toBe('L2 Contract');
    expect(tierOf('L3:create product')).toBe('L3 Data');
    expect(checkLabel('L2:create product')).toBe('create product');
  });

  it('maps results and verdicts to indicators', () => {
    expect(indicatorOf('pass')).toBe('success');
    expect(indicatorOf('fail')).toBe('error');
    expect(indicatorOf('warn')).toBe('warning');
    expect(indicatorOf('skip')).toBe('stopped');
    expect(indicatorOf('not_deployed')).toBe('pending');
    expect(verdictIndicator('pass_with_warnings')).toBe('warning');
  });

  it('summarizes counts, tiers and failures', () => {
    const s = summarize('fail', [
      { check: 'database_live', result: 'pass' },
      { check: 'L1:health', result: 'fail', detail: 'GET /products → 500' },
      { check: 'L2:list', result: 'fail' },
      { check: 'cicd_coverage_gate', result: 'warn' },
    ]);
    expect(s.verdict).toBe('fail');
    expect(s.counts).toEqual({ pass: 1, fail: 2, warn: 1 });
    expect(s.byTier['L0 Platform'].map((c) => c.check)).toEqual(['database_live', 'cicd_coverage_gate']);
    expect(s.failed.map((c) => c.check)).toEqual(['L1:health', 'L2:list']);
    expect(summarize(undefined, undefined).verdict).toBe('unknown');
  });
});


describe('run history + links', () => {
  it('diffs two runs', () => {
    const prev = [{ check: 'L1:probe', result: 'fail' }, { check: 'database_live', result: 'pass' }];
    const cur = [{ check: 'L1:health', result: 'pass' }, { check: 'database_live', result: 'pass' }];
    expect(diffRuns(prev, cur)).toEqual([
      { check: 'L1:health', before: undefined, after: 'pass' },
      { check: 'L1:probe', before: 'fail' },
    ]);
  });

  it('builds console links per compute', () => {
    const ec2 = consoleLinks('us-east-1', 'APP-TE', 'ec2', { pipeline_name: 'APP-TE-pipeline' }, 'pe-1');
    expect(ec2[0].href).toContain('/codepipeline/pipelines/APP-TE-pipeline/executions/pe-1/timeline');
    expect(ec2.map((l) => l.label)).toEqual(['Pipeline run', 'CodeDeploy deployments', 'Auto Scaling group', 'CloudWatch logs']);
    expect(consoleLinks('us-east-1', 'APP-HE', 'ecs_fargate', {}).map((l) => l.label)).toContain('ECS service');
    expect(consoleLinks('', 'APP-HE', 'lambda', {})).toEqual([]);
  });
});
