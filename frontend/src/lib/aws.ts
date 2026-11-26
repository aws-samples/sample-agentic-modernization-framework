// AWS environment helpers driven by GET /system/capabilities.
// The console domain lives here and nowhere else; regions and poll cadences come
// from the backend, with the fallbacks below only for a missing capabilities key.
import type { Capabilities } from '../api/system';

const CONSOLE_DOMAIN = 'console.aws.amazon.com';

/** AWS console deep link for `path` (should start with '/') in `region`. '' when the region is unknown. */
export function consoleUrl(region: string | undefined, path: string): string {
  if (!region) return '';
  return `https://${region}.${CONSOLE_DOMAIN}${path.startsWith('/') ? path : `/${path}`}`;
}

// Display names only (the list of offered regions comes from capabilities).
const REGION_NAMES: Record<string, string> = {
  'us-east-1': 'N. Virginia',
  'us-east-2': 'Ohio',
  'us-west-1': 'N. California',
  'us-west-2': 'Oregon',
  'eu-west-1': 'Ireland',
  'eu-central-1': 'Frankfurt',
  'ap-southeast-1': 'Singapore',
  'ap-southeast-2': 'Sydney',
  'ap-northeast-1': 'Tokyo',
};

export interface RegionOption { value: string; label: string }

type RegionCaps = Pick<Capabilities, 'aws_region' | 'supported_target_regions'>;

/** Target regions the deployment supports: `supported_target_regions`, else the control-plane region. */
export function targetRegions(caps: RegionCaps): string[] {
  const list = (caps.supported_target_regions || []).filter((r) => typeof r === 'string' && r);
  if (list.length > 0) return Array.from(new Set(list));
  return caps.aws_region ? [caps.aws_region] : [];
}

export function regionLabel(region: string): string {
  const name = REGION_NAMES[region];
  return name ? `${region} (${name})` : region;
}

export function targetRegionOptions(caps: RegionCaps): RegionOption[] {
  return targetRegions(caps).map((value) => ({ value, label: regionLabel(value) }));
}

// Fallbacks = the values the UI used before they moved to the backend.
export const DEFAULT_VALIDATION_POLL_MS = 15000;
export const DEFAULT_VALIDATION_POLL_MAX = 60;
export const DEFAULT_WORKFLOW_POLL_MS = 10000;

/** A positive number from capabilities, else `fallback`. */
export function positiveOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback;
}
