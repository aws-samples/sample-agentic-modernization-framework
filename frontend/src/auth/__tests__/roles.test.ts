import { describe, expect, it } from 'vitest';
import { Role, hasRole, permissionHint, requiredRoleForGate, roleFromGroups } from '../roles';

describe('roleFromGroups (mirrors backend rbac.GROUP_TO_ROLE)', () => {
  it('takes the highest mapped group', () => {
    expect(roleFromGroups(['viewers', 'migration-engineers'])).toBe(Role.ENGINEER);
    expect(roleFromGroups(['admins', 'migration-leads', 'migration-engineers', 'viewers'])).toBe(Role.ADMIN);
    expect(roleFromGroups(['Admin'])).toBe(Role.ADMIN);
    expect(roleFromGroups(['engineer'])).toBe(Role.ENGINEER);
  });

  it('fails closed to Viewer for no/unknown groups', () => {
    expect(roleFromGroups(undefined)).toBe(Role.VIEWER);
    expect(roleFromGroups([])).toBe(Role.VIEWER);
    expect(roleFromGroups(['migration-leads', 'ADMINS', 'administrators'])).toBe(Role.VIEWER);
  });
});

describe('hasRole', () => {
  it('is ordered Viewer < Engineer < Admin', () => {
    expect(hasRole(['viewers'], Role.VIEWER)).toBe(true);
    expect(hasRole(['viewers'], Role.ENGINEER)).toBe(false);
    expect(hasRole(['migration-engineers'], Role.ENGINEER)).toBe(true);
    expect(hasRole(['migration-engineers'], Role.ADMIN)).toBe(false);
    expect(hasRole(['admins'], Role.ENGINEER)).toBe(true);
    expect(hasRole(['admins'], Role.ADMIN)).toBe(true);
  });
});

describe('requiredRoleForGate', () => {
  it('needs Admin for the deploy gate only when deploy is enabled, Engineer elsewhere', () => {
    expect(requiredRoleForGate('execution_review', true)).toBe(Role.ADMIN);
    expect(requiredRoleForGate('execution_review', false)).toBe(Role.ENGINEER);
    expect(requiredRoleForGate('execution_review')).toBe(Role.ENGINEER);
    for (const step of ['discovery_review', 'assessment_review', 'design_review', 'validation_review',
      'cutover_review', 'handover_review']) {
      expect(requiredRoleForGate(step)).toBe(Role.ENGINEER);
    }
    expect(permissionHint(Role.ADMIN)).toBe('Requires the Admin role.');
  });
});
