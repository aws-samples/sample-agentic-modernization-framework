import { describe, it, expect } from 'vitest';
import { dbInstanceClassesFor, requiresAmi, requiresDbInstanceClass, validateAssessmentInputs } from '../strategyOptions';

const base = { target_account: '123456789012', vpc_cidr: '10.0.0.0/16', cicd_target: 'codepipeline' };
const fields = (errs: { field: string }[]) => new Set(errs.map((e) => e.field));

describe('conditional assessment sizing inputs', () => {
  it('asks for an AMI only for EC2', () => {
    expect(requiresAmi('ec2')).toBe(true);
    expect(requiresAmi('ecs_fargate')).toBe(false);
    const ec2 = { ...base, target_compute: 'ec2', target_database: 'same_as_source' };
    expect(fields(validateAssessmentInputs('rehost', ec2)).has('ami_id')).toBe(true);
    expect(fields(validateAssessmentInputs('rehost', { ...ec2, ami_id: 'ami-bad' })).has('ami_id')).toBe(true);
    expect(validateAssessmentInputs('rehost', { ...ec2, ami_id: 'ami-0123456789abcdef0' })).toEqual([]);
  });

  it('asks for an engine-appropriate DB instance class only for managed engines', () => {
    expect(requiresDbInstanceClass('same_as_source')).toBe(false);
    expect(requiresDbInstanceClass('aurora_mysql')).toBe(true);
    expect(dbInstanceClassesFor('rds_sqlserver')).toEqual([]); // out of scope
    expect(dbInstanceClassesFor('aurora_postgresql').map((o) => o.value)).not.toContain('db.m6g.large');
    const rds = { ...base, target_compute: 'ecs_fargate', target_database: 'rds_postgresql' };
    expect(fields(validateAssessmentInputs('replatform', rds)).has('db_instance_class')).toBe(true);
    expect(validateAssessmentInputs('replatform', { ...rds, db_instance_class: 'db.r6g.large' })).toEqual([]);
  });
});

describe('vpc_cidr validation', () => {
  it('rejects host bits and prefixes CreateVpc refuses (APP-HE/APP-TE 2026-10-13)', () => {
    const ecs = { ...base, target_compute: 'ecs_fargate', target_database: 'dynamodb' };
    for (const bad of ['10.0.0.2/24', '10.0.0.1/24', '10.0.0.0/25', '192.168.0.0/28', '10.0.0.0/29', '10.0.0.0/30']) {
      expect(fields(validateAssessmentInputs('replatform', { ...ecs, vpc_cidr: bad })).has('vpc_cidr')).toBe(true);
    }
    for (const ok of ['10.0.0.0/16', '10.20.4.0/24', '10.8.0.0/21', '192.168.0.0/24']) {
      expect(fields(validateAssessmentInputs('replatform', { ...ecs, vpc_cidr: ok })).has('vpc_cidr')).toBe(false);
    }
  });
});
