// Phase_Mapping_Source (Requirements 11.2, 11.3).
// Single authority for the ordered phase list and for mapping raw backend
// phase/step identifiers to a canonical UI Phase.

import type { Phase } from './types';

export interface PhaseDef {
  id: Phase;
  label: string;
}

/** Ordered core phases shown as columns. Cutover is conditional per strategy. */
export const PHASES: PhaseDef[] = [
  { id: 'discovery', label: 'Discovery' },
  { id: 'assessment', label: 'Assessment' },
  { id: 'design', label: 'Design' },
  { id: 'execution', label: 'Execution' },
  { id: 'validation', label: 'Validation' },
  { id: 'cutover', label: 'Cutover' },
  { id: 'handover', label: 'Handover' },
];

// Raw backend phase/step aliases → canonical UI phase.
const PHASE_ALIASES: Record<string, Phase> = {
  discovery: 'discovery',
  assessment: 'assessment',
  design: 'design',
  migration_plan: 'design',
  planning: 'design',
  portfolio_complete: 'design',
  execution: 'execution',
  transform: 'execution',
  infrastructure: 'execution',
  database_migration: 'execution',
  cicd: 'execution',
  // Deploy = the execution_review approval applying the reviewed commit; Validation comes after.
  deploy: 'execution',
  validation: 'validation',
  cutover: 'cutover',
  handover: 'handover',
  post_deploy: 'handover',
};

/** Map a raw backend phase or step identifier to a canonical Phase (or undefined). */
export function phaseOf(raw?: string): Phase | undefined {
  if (!raw) return undefined;
  const key = raw.toLowerCase();
  if (PHASE_ALIASES[key]) return PHASE_ALIASES[key];
  // step ids like "assessment_review" → strip suffix and re-map
  const base = key.replace(/_(review|approval|running|approve)$/,'');
  return PHASE_ALIASES[base];
}

/** UI column index for a phase (or -1). `completed` returns past-all. */
export function getPhaseIndex(raw?: string): number {
  if (!raw) return -1;
  if (raw.toLowerCase() === 'completed') return PHASES.length;
  const phase = phaseOf(raw);
  return phase ? PHASES.findIndex((p) => p.id === phase) : -1;
}
