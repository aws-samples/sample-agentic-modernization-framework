// View model for the Validation phase: the deterministic checks tool_execution.validate_deployment
// stores as ANALYSIS#validation-results (L0 platform + L1 smoke / L2 contract / L3 data) and the
// deploy record (ANALYSIS#deploy). Pure, so it is unit-tested; ValidationResults.tsx renders it.
import { consoleUrl } from '../lib/aws';

export type CheckResult = 'pass' | 'fail' | 'warn' | 'skip' | 'not_deployed' | string;

export interface ValidationCheck {
  check: string;
  result: CheckResult;
  detail?: string;
}

export type Tier = 'L0 Platform' | 'L1 Smoke' | 'L2 Contract' | 'L3 Data';

export const TIERS: Tier[] = ['L0 Platform', 'L1 Smoke', 'L2 Contract', 'L3 Data'];

/** L1:/L2:/L3: prefixes are the app checks (validation_probe); everything else is L0 platform. */
export function tierOf(check: string): Tier {
  if (check.startsWith('L1:')) return 'L1 Smoke';
  if (check.startsWith('L2:')) return 'L2 Contract';
  if (check.startsWith('L3:')) return 'L3 Data';
  return 'L0 Platform';
}

/** The check name without its tier prefix ("L2:create product" → "create product"). */
export function checkLabel(check: string): string {
  return check.replace(/^L[123]:/, '');
}

export type IndicatorType = 'success' | 'error' | 'warning' | 'stopped' | 'pending' | 'info';

export function indicatorOf(result: CheckResult): IndicatorType {
  switch (result) {
    case 'pass': return 'success';
    case 'fail': return 'error';
    case 'warn': return 'warning';
    case 'skip': return 'stopped';
    case 'not_deployed': return 'pending';
    default: return 'info';
  }
}

export interface ValidationSummary {
  verdict: string;
  counts: Record<string, number>;
  byTier: Record<Tier, ValidationCheck[]>;
  failed: ValidationCheck[];
}

export function summarize(verdict: string | undefined, checks: ValidationCheck[] | undefined): ValidationSummary {
  const byTier = Object.fromEntries(TIERS.map((t) => [t, [] as ValidationCheck[]])) as Record<Tier, ValidationCheck[]>;
  const counts: Record<string, number> = {};
  for (const c of checks || []) {
    byTier[tierOf(c.check)].push(c);
    counts[c.result] = (counts[c.result] || 0) + 1;
  }
  return {
    verdict: verdict || 'unknown',
    counts,
    byTier,
    failed: (checks || []).filter((c) => c.result === 'fail'),
  };
}

export function verdictIndicator(verdict: string): IndicatorType {
  if (verdict === 'pass') return 'success';
  if (verdict === 'pass_with_warnings') return 'warning';
  if (verdict === 'fail') return 'error';
  return 'pending';
}

// ── run history ───────────────────────────────────────────────────────────────

export interface CheckChange {
  check: string;
  before?: CheckResult;
  after?: CheckResult;
}

/** Checks whose result differs between two runs (added / removed included). */
export function diffRuns(previous: ValidationCheck[] | undefined, current: ValidationCheck[] | undefined): CheckChange[] {
  const prev = new Map((previous || []).map((c) => [c.check, c.result]));
  const cur = new Map((current || []).map((c) => [c.check, c.result]));
  const out: CheckChange[] = [];
  for (const [check, after] of cur) {
    const before = prev.get(check);
    if (before !== after) out.push({ check, before, after });
  }
  for (const [check, before] of prev) if (!cur.has(check)) out.push({ check, before });
  return out;
}

// ── console links ─────────────────────────────────────────────────────────────

export interface ConsoleLink { label: string; href: string }

/** AWS console deep links for a deployed app (region from useCapabilities, names from the AMF conventions). */
export function consoleLinks(region: string, appId: string, compute: string | undefined,
                             outputs: Record<string, unknown>, pipelineExecutionId?: string): ConsoleLink[] {
  if (!region) return [];
  const pipeline = String(outputs.pipeline_name || `${appId}-pipeline`);
  const links: ConsoleLink[] = [
    pipelineExecutionId
      ? { label: 'Pipeline run', href: consoleUrl(region, `/codesuite/codepipeline/pipelines/${pipeline}/executions/${pipelineExecutionId}/timeline?region=${region}`) }
      : { label: 'Pipeline', href: consoleUrl(region, `/codesuite/codepipeline/pipelines/${pipeline}/view?region=${region}`) },
  ];
  if (compute === 'ec2') {
    links.push({ label: 'CodeDeploy deployments', href: consoleUrl(region, `/codesuite/codedeploy/deployments?region=${region}`) });
    links.push({ label: 'Auto Scaling group', href: consoleUrl(region, `/ec2/home?region=${region}#AutoScalingGroupDetails:id=${appId}-asg`) });
  } else if (compute === 'ecs_fargate') {
    links.push({ label: 'ECS service', href: consoleUrl(region, `/ecs/v2/clusters/${appId}-cluster/services/${appId}-service/health?region=${region}`) });
  } else if (compute === 'lambda') {
    links.push({ label: 'Lambda functions', href: consoleUrl(region, `/lambda/home?region=${region}#/functions?fo=and&o0=%3A&v0=${appId}-`) });
  }
  links.push({ label: 'CloudWatch logs', href: consoleUrl(region, `/cloudwatch/home?region=${region}#logsV2:log-groups$3FlogGroupNameFilter$3D${appId}`) });
  return links;
}
