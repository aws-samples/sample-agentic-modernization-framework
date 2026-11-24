// Single source of truth for which input values are valid per 7R strategy.
// Shared by the assessment-approval modal (`inputs` form) and the edit modal so
// the user can never pick a compute/DB/CI-CD/transformation that contradicts the
// chosen strategy. Mirror of backend/src/core/strategy_options.py — keep in sync.

export type Strategy =
  | 'rehost' | 'relocate' | 'replatform' | 'refactor'
  | 'repurchase' | 'retire' | 'retain';

export interface Opt { value: string; label: string }

// Strategies the workflow closes out (ResolveStrategy → Handover): no AWS target.
// Scope: ECS Fargate + Lambda (EC2 for rehost/relocate only),
// DynamoDB / RDS / Aurora (PostgreSQL, MySQL), CI/CD on AWS CodePipeline only.
const CLOSE_OUT = new Set(['retire', 'retain', 'repurchase']);

// ── compute (target_compute) ──────────────────────────────────────────────────
const COMPUTE_LIFT = [{ value: 'ec2', label: 'EC2 (Virtual Machines)' }];
const COMPUTE_MODERN: Opt[] = [
  { value: 'ecs_fargate', label: 'ECS Fargate (Serverless Containers)' },
  { value: 'lambda', label: 'Lambda (Serverless Functions)' },
];
const COMPUTE_NONE: Opt[] = [{ value: 'none', label: '— Not applicable —' }];

export function computeOptionsFor(strategy?: string | null): Opt[] {
  const s = (strategy || '').toLowerCase() as Strategy;
  if (s === 'rehost' || s === 'relocate') return COMPUTE_LIFT;
  if (s === 'replatform' || s === 'refactor') return COMPUTE_MODERN;
  if (CLOSE_OUT.has(s)) return COMPUTE_NONE;
  return COMPUTE_MODERN;
}

// ── database (target_database) ────────────────────────────────────────────────
const DB_LIFT_AS_IS: Opt[] = [
  { value: 'same_as_source', label: 'Same as source (lift & shift DB)' },
  // Same engine onto a managed instance (MySQL source): the app only gets a new endpoint.
  { value: 'rds_mysql', label: 'RDS MySQL (same engine, managed)' },
  { value: 'dynamodb', label: 'DynamoDB' },
];
const DB_MODERN: Opt[] = [
  { value: 'aurora_postgresql', label: 'Aurora PostgreSQL' },
  { value: 'aurora_mysql', label: 'Aurora MySQL' },
  { value: 'rds_postgresql', label: 'RDS PostgreSQL' },
  { value: 'rds_mysql', label: 'RDS MySQL' },
  { value: 'dynamodb', label: 'DynamoDB' },
  { value: 'none', label: 'None (stateless)' },
];
const DB_NONE: Opt[] = [{ value: 'none', label: '— Not applicable —' }];

export function databaseOptionsFor(strategy?: string | null): Opt[] {
  const s = (strategy || '').toLowerCase() as Strategy;
  if (s === 'rehost' || s === 'relocate') return DB_LIFT_AS_IS;
  if (s === 'replatform' || s === 'refactor') return DB_MODERN;
  if (CLOSE_OUT.has(s)) return DB_NONE;
  return DB_MODERN;
}

// ── CI/CD (cicd_target) ───────────────────────────────────────────────────────
const CICD_FULL: Opt[] = [
  { value: 'codepipeline', label: 'AWS CodePipeline' },
];
const CICD_NONE: Opt[] = [{ value: 'none', label: '— Not applicable —' }];

export function cicdOptionsFor(strategy?: string | null): Opt[] {
  const s = (strategy || '').toLowerCase() as Strategy;
  if (CLOSE_OUT.has(s)) return CICD_NONE;
  return CICD_FULL;
}

// ── Transformations ───────────────────────────────────────────────────────────
// Transforms are code changes — only valid for replatform/refactor. Everything
// else (rehost/relocate/retire/retain/repurchase) runs the code AS-IS → zero
// transforms. The frontend uses this to hide the control; the backend enforces
// it deterministically in tool_documents.store_analysis_result.
export function transformsAllowedFor(strategy?: string | null): boolean {
  const s = (strategy || '').toLowerCase();
  return s === 'replatform' || s === 'refactor';
}

// ── Required-input gating ─────────────────────────────────────────────────────
// AWS Account ID and VPC CIDR are only meaningful when the migration lands in an
// AWS account with a VPC — i.e. not for close-outs (decommission, kept on-prem,
// external SaaS). Everything else requires both.
export function requiresAwsTargeting(strategy?: string | null): boolean {
  return !CLOSE_OUT.has((strategy || '').toLowerCase());
}

// ── Conditional sizing inputs (assessment approval) ──────────────────────────
// EC2 targets launch from an AMI (MGN-launched or golden image); managed DB
// targets need an instance class — valid families differ per engine (Aurora:
// r/t only). Mirror of strategy_options.py.
const MANAGED_DB = new Set(['rds_postgresql', 'rds_mysql', 'aurora_postgresql', 'aurora_mysql']);
const DB_CLASSES_GRAVITON = ['db.t4g.medium', 'db.t4g.large', 'db.m6g.large', 'db.m6g.xlarge', 'db.m6g.2xlarge',
  'db.r6g.large', 'db.r6g.xlarge', 'db.r6g.2xlarge', 'db.r6g.4xlarge'];
const DB_CLASSES_AURORA = ['db.t4g.medium', 'db.t4g.large', 'db.r6g.large', 'db.r6g.xlarge', 'db.r6g.2xlarge',
  'db.r6g.4xlarge'];

export const requiresAmi = (targetCompute?: string | null): boolean => (targetCompute || '').toLowerCase() === 'ec2';
export const requiresDbInstanceClass = (targetDatabase?: string | null): boolean =>
  MANAGED_DB.has((targetDatabase || '').toLowerCase());

export function dbInstanceClassesFor(targetDatabase?: string | null): Opt[] {
  const td = (targetDatabase || '').toLowerCase();
  const list = td.startsWith('aurora_') ? DB_CLASSES_AURORA
    : MANAGED_DB.has(td) ? DB_CLASSES_GRAVITON : [];
  return list.map((v) => ({ value: v, label: v }));
}

// ── Validators (shared with backend) ─────────────────────────────────────────
const AWS_ACCOUNT_ID_RE = /^\d{12}$/;
// An MGN/golden AMI id, or "al2023": a fresh Amazon Linux 2023 instance bootstrapped by the ec2 module.
const AMI_ID_RE = /^(?:ami-(?:[0-9a-f]{8}|[0-9a-f]{17})|al2023)$/;
// RFC1918 ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16. VPC prefix /16–/24 (the generated VPC needs room
// for 3 tiers × AZs of subnets ≥ AWS's /28 minimum; mirrors core/strategy_options.py).
const VPC_CIDR_RE = /^(?:10\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}|192\.168\.\d{1,3})\.\d{1,3}\/(?:1[6-9]|2[0-4])$/;

/** True when the CIDR is a network address (no host bits): 10.0.0.0/24 yes, 10.0.0.1/24 no. */
export function isNetworkAddress(cidr: string): boolean {
  const [ip, bits] = cidr.split('/');
  const prefix = Number(bits);
  const octets = (ip || '').split('.').map(Number);
  if (octets.length !== 4 || octets.some((o) => !Number.isInteger(o) || o < 0 || o > 255)) return false;
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) return false;
  const addr = ((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) >>> 0;
  const hostMask = prefix === 32 ? 0 : (0xffffffff >>> prefix);
  return (addr & hostMask) === 0;
}

export interface ValidationError { field: string; message: string }

/** Validate the inputs the user submits at assessment approval. Returns the
 *  list of blocking errors; empty = ok. */
export function validateAssessmentInputs(
  strategy: string | null | undefined,
  inputs: {
    target_compute?: string;
    target_database?: string;
    cicd_target?: string;
    target_account?: string;
    vpc_cidr?: string;
    ami_id?: string;
    db_instance_class?: string;
  },
): ValidationError[] {
  const errs: ValidationError[] = [];
  const s = (strategy || '').toLowerCase();

  const validCompute = new Set(computeOptionsFor(s).map((o) => o.value));
  if (inputs.target_compute && !validCompute.has(inputs.target_compute)) {
    errs.push({ field: 'target_compute',
      message: `target_compute='${inputs.target_compute}' is not valid for strategy='${s}'. Allowed: ${[...validCompute].join(', ')}.` });
  }

  const validDb = new Set(databaseOptionsFor(s).map((o) => o.value));
  if (inputs.target_database && !validDb.has(inputs.target_database)) {
    errs.push({ field: 'target_database',
      message: `target_database='${inputs.target_database}' is not valid for strategy='${s}'. Allowed: ${[...validDb].join(', ')}.` });
  }

  const validCicd = new Set(cicdOptionsFor(s).map((o) => o.value));
  if (inputs.cicd_target && !validCicd.has(inputs.cicd_target)) {
    errs.push({ field: 'cicd_target',
      message: `cicd_target='${inputs.cicd_target}' is not valid for strategy='${s}'.` });
  }

  if (requiresAwsTargeting(s)) {
    if (!inputs.target_account) {
      errs.push({ field: 'target_account', message: 'AWS Account ID is required for this strategy.' });
    } else if (!AWS_ACCOUNT_ID_RE.test(inputs.target_account)) {
      errs.push({ field: 'target_account', message: 'AWS Account ID must be exactly 12 digits.' });
    }
    if (!inputs.vpc_cidr) {
      errs.push({ field: 'vpc_cidr', message: 'VPC CIDR is required for this strategy.' });
    } else if (!VPC_CIDR_RE.test(inputs.vpc_cidr) || !isNetworkAddress(inputs.vpc_cidr)) {
      errs.push({ field: 'vpc_cidr', message: 'VPC CIDR must be an RFC1918 /16–/24 network address (e.g. 10.0.0.0/16, not 10.0.0.1/24).' });
    }
  }

  if (requiresAmi(inputs.target_compute)) {
    const ami = (inputs.ami_id || '').trim();
    if (!ami) {
      errs.push({ field: 'ami_id', message: 'AMI ID is required for an EC2 target (the MGN-launched or golden AMI).' });
    } else if (!AMI_ID_RE.test(ami)) {
      errs.push({ field: 'ami_id', message: 'AMI ID must look like ami-0123456789abcdef0 (8 or 17 hex characters), or be al2023.' });
    }
  }

  if (requiresDbInstanceClass(inputs.target_database)) {
    const cls = (inputs.db_instance_class || '').trim();
    const allowed = dbInstanceClassesFor(inputs.target_database).map((o) => o.value);
    if (!cls) {
      errs.push({ field: 'db_instance_class', message: `DB instance class is required for target_database='${inputs.target_database}'.` });
    } else if (!allowed.includes(cls)) {
      errs.push({ field: 'db_instance_class', message: `db_instance_class='${cls}' is not valid for ${inputs.target_database}. Allowed: ${allowed.join(', ')}.` });
    }
  }

  return errs;
}
