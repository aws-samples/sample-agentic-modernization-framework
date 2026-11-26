// Readiness questionnaires for migration risk scoring.
// Mirrors backend/src/core/risk_scoring.py — keep keys + options in sync.

export const STATUS_OPTIONS = ['Completed', 'In Progress', 'Initiated', 'Pending', 'Not Started'];

export const BUSINESS_QUESTIONS: [string, string][] = [
  ['stakeholder_approval', 'Stakeholder approval'],
  ['cost_approval', 'Cost / budget approval'],
  ['dr_signoff', 'DR / business-continuity sign-off'],
  ['compliance_signoff', 'Compliance / security sign-off'],
  ['migration_window', 'Migration window agreed'],
];

export const TEAM_QUESTIONS: [string, string][] = [
  ['team_availability', 'Team availability for migration'],
  ['knowledge_transfer', 'Knowledge transfer completed'],
  ['aws_skills', 'AWS skills / training'],
  ['runbook_ownership', 'Runbook & ownership defined'],
  ['tooling_access', 'Tooling & access provisioned'],
];
