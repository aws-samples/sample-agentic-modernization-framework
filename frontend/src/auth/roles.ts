/**
 * Client-side mirror of the backend RBAC (`backend/src/auth/rbac.py`): Viewer < Engineer < Admin.
 *
 * This only drives UI affordances (disabled buttons + tooltips). The backend is the
 * enforcement point — a stale or forged role here just yields a 403 from the API.
 */

export enum Role {
  VIEWER = 0,
  ENGINEER = 1,
  ADMIN = 2,
}

/** Cognito group → role. Keep in sync with `GROUP_TO_ROLE` in rbac.py (exact, case-sensitive keys). */
export const GROUP_TO_ROLE: Readonly<Record<string, Role>> = {
  Viewer: Role.VIEWER,
  viewer: Role.VIEWER,
  viewers: Role.VIEWER,
  Engineer: Role.ENGINEER,
  engineer: Role.ENGINEER,
  'migration-engineers': Role.ENGINEER,
  Admin: Role.ADMIN,
  admin: Role.ADMIN,
  admins: Role.ADMIN,
};

export const ROLE_LABELS: Readonly<Record<Role, string>> = {
  [Role.VIEWER]: 'Viewer',
  [Role.ENGINEER]: 'Engineer',
  [Role.ADMIN]: 'Admin',
};

/** Highest role among the user's groups; unknown groups and no groups count as Viewer (as on the backend). */
export function roleFromGroups(groups: readonly string[] | null | undefined): Role {
  let role = Role.VIEWER;
  for (const g of groups || []) {
    const r = GROUP_TO_ROLE[g];
    if (r !== undefined && r > role) role = r;
  }
  return role;
}

export function hasRole(groups: readonly string[] | null | undefined, minimum: Role): boolean {
  return roleFromGroups(groups) >= minimum;
}

/** Minimum role to approve/retry a gate: the deploy gate (`execution_review`) needs Admin. */
/** Approving execution_review deploys (Admin) — only when deploy is enabled (capabilities.deploy_enabled). */
export function requiredRoleForGate(step: string | null | undefined, deployEnabled = false): Role {
  return step === 'execution_review' && deployEnabled ? Role.ADMIN : Role.ENGINEER;
}

/** Tooltip/inline text for a control disabled because the user lacks `minimum`. */
export function permissionHint(minimum: Role): string {
  return `Requires the ${ROLE_LABELS[minimum]} role.`;
}
