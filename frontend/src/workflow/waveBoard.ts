// Wave board + declared dependency helpers. Pure, so they're unit-tested.
// Wave numbers follow the planning playbook: 0 = Pilot, 1 = Foundation, 2 = Core, 3 = Customer-Facing,
// higher = spill-over / Long Tail. An app with no wave_number is Unassigned (UNASSIGNED = -1 on the board).

export const UNASSIGNED = -1;
const BAND_NAMES: Record<number, string> = { 0: 'Pilot', 1: 'Foundation', 2: 'Core', 3: 'Customer-Facing' };

/** META wave_number ('' / missing → Unassigned). */
export function parseWave(v: unknown): number {
  if (v === undefined || v === null || String(v).trim() === '') return UNASSIGNED;
  const n = parseInt(String(v), 10);
  return Number.isFinite(n) && n >= 0 ? n : UNASSIGNED;
}

/** Value to save for a board column ('' clears the assignment). */
export function waveValue(n: number): number | string {
  return n === UNASSIGNED ? '' : n;
}

/** Highest wave any app is in (0 when none are assigned). */
export function highestWave(assignments: Record<string, number>): number {
  return Math.max(0, ...Object.values(assignments).filter((w) => Number.isFinite(w) && w >= 0));
}

/** Columns to render: Unassigned, then 0 (Pilot) through max(3, highest used, the user's added waves). */
export function visibleWaves(assignments: Record<string, number>, shownMax: number): number[] {
  const top = Math.max(3, highestWave(assignments), shownMax);
  return [UNASSIGNED, ...Array.from({ length: top + 1 }, (_, i) => i)];
}

/** The board may drop its last wave only when it is empty, above wave 1 and above any used wave. */
export function canRemoveLastWave(assignments: Record<string, number>, shownMax: number): boolean {
  const top = Math.max(3, highestWave(assignments), shownMax);
  return top > 3 && top > highestWave(assignments);
}

export function waveLabel(n: number): string {
  if (n === UNASSIGNED) return 'Unassigned';
  return BAND_NAMES[n] ? `Wave ${n} · ${BAND_NAMES[n]}` : `Wave ${n}`;
}

/** depends_on after adding / removing one target (deduplicated, never the app itself). */
export function withDependency(current: string[] | undefined, appId: string, target: string): string[] {
  const list = (current || []).filter(Boolean);
  if (!target || target === appId || list.includes(target)) return list;
  return [...list, target];
}

export function withoutDependency(current: string[] | undefined, target: string): string[] {
  return (current || []).filter((d) => d !== target);
}

export function isDeclared(types: string[] | undefined): boolean {
  return (types || []).includes('declared');
}
