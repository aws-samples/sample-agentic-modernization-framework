import { describe, it, expect } from 'vitest';
import { visibleWaves, canRemoveLastWave, highestWave, waveLabel, withDependency, withoutDependency, isDeclared, parseWave, waveValue, UNASSIGNED } from '../waveBoard';

describe('wave board', () => {
  it('shows Unassigned plus the playbook bands 0..3, more once added', () => {
    expect(visibleWaves({ a: 0, b: 1 }, 0)).toEqual([UNASSIGNED, 0, 1, 2, 3]);
    expect(visibleWaves({ a: 0 }, 5)).toEqual([UNASSIGNED, 0, 1, 2, 3, 4, 5]);
  });
  it('always shows the highest used wave', () => {
    expect(visibleWaves({ a: 6 }, 0)).toEqual([UNASSIGNED, 0, 1, 2, 3, 4, 5, 6]);
    expect(highestWave({ a: UNASSIGNED })).toBe(0);
  });
  it('removes only an empty trailing wave above the bands', () => {
    expect(canRemoveLastWave({ a: 1 }, 3)).toBe(false);
    expect(canRemoveLastWave({ a: 1 }, 5)).toBe(true);
    expect(canRemoveLastWave({ a: 5 }, 5)).toBe(false);
  });
  it('labels waves with the playbook bands', () => {
    expect(waveLabel(UNASSIGNED)).toBe('Unassigned');
    expect(waveLabel(0)).toBe('Wave 0 · Pilot');
    expect(waveLabel(2)).toBe('Wave 2 · Core');
    expect(waveLabel(6)).toBe('Wave 6');
  });
  it('parses META wave_number and saves Unassigned as a cleared value', () => {
    expect(parseWave(undefined)).toBe(UNASSIGNED);
    expect(parseWave('')).toBe(UNASSIGNED);
    expect(parseWave('0')).toBe(0);
    expect(parseWave('4')).toBe(4);
    expect(waveValue(UNASSIGNED)).toBe('');
    expect(waveValue(2)).toBe(2);
  });
});

describe('declared dependencies', () => {
  it('adds without duplicates or self-links', () => {
    expect(withDependency(['B'], 'A', 'C')).toEqual(['B', 'C']);
    expect(withDependency(['B'], 'A', 'B')).toEqual(['B']);
    expect(withDependency([], 'A', 'A')).toEqual([]);
  });
  it('removes a target', () => {
    expect(withoutDependency(['B', 'C'], 'B')).toEqual(['C']);
  });
  it('detects declared edges', () => {
    expect(isDeclared(['tcp', 'declared'])).toBe(true);
    expect(isDeclared(['tcp'])).toBe(false);
  });
});
