import { describe, it, expect } from 'vitest';
import { consoleUrl, targetRegions, targetRegionOptions, regionLabel, positiveOr } from '../aws';

describe('consoleUrl', () => {
  it('builds a regional console URL', () => {
    expect(consoleUrl('eu-west-1', '/states/home?region=eu-west-1')).toBe(
      'https://eu-west-1.console.aws.amazon.com/states/home?region=eu-west-1');
  });
  it('adds a leading slash when missing', () => {
    expect(consoleUrl('us-west-2', 'ecs/v2')).toBe('https://us-west-2.console.aws.amazon.com/ecs/v2');
  });
  it('returns empty when the region is unknown', () => {
    expect(consoleUrl('', '/states/home')).toBe('');
    expect(consoleUrl(undefined, '/states/home')).toBe('');
  });
});

describe('targetRegions', () => {
  it('uses supported_target_regions from capabilities (deduplicated)', () => {
    expect(targetRegions({ aws_region: 'us-east-1', supported_target_regions: ['eu-west-1', 'us-west-2', 'eu-west-1'] }))
      .toEqual(['eu-west-1', 'us-west-2']);
  });
  it('falls back to the control-plane region when the key is missing or empty', () => {
    expect(targetRegions({ aws_region: 'eu-central-1' })).toEqual(['eu-central-1']);
    expect(targetRegions({ aws_region: 'eu-central-1', supported_target_regions: [] })).toEqual(['eu-central-1']);
  });
  it('is empty before capabilities load (no region)', () => {
    expect(targetRegions({ aws_region: '' })).toEqual([]);
  });
  it('labels known regions and passes unknown ones through', () => {
    expect(regionLabel('us-west-2')).toBe('us-west-2 (Oregon)');
    expect(regionLabel('me-south-1')).toBe('me-south-1');
    expect(targetRegionOptions({ aws_region: 'us-west-2' })).toEqual([{ value: 'us-west-2', label: 'us-west-2 (Oregon)' }]);
  });
});

describe('positiveOr', () => {
  it('keeps a positive number, else the fallback', () => {
    expect(positiveOr(5000, 10000)).toBe(5000);
    expect(positiveOr(undefined, 10000)).toBe(10000);
    expect(positiveOr(0, 10000)).toBe(10000);
    expect(positiveOr('5000', 10000)).toBe(10000);
  });
});
