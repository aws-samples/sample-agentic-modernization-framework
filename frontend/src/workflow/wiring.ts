// Runtime wiring confirmed at the assessment gate: what Execution
// builds the IaC + pipeline from. Values come from META, then the repo scan's detected_*.

/** Container port when neither META nor the repo scan has one (mirrors the backend default). */
export const DEFAULT_CONTAINER_PORT = '8080';

interface WiringField { key: string; label: string; fallback: string }

export function wiringFields(compute?: string, database?: string): WiringField[] {
  const out: WiringField[] = [{ key: 'build_dir', label: 'Build directory', fallback: 'repo root' }];
  if (compute === 'ecs_fargate') {
    out.push({ key: 'container_port', label: 'Container port', fallback: DEFAULT_CONTAINER_PORT },
      { key: 'health_check_path', label: 'Health check path', fallback: '/health' });
  }
  if (compute === 'lambda') {
    out.push({ key: 'lambda_handler', label: 'Lambda handler', fallback: 'language default' },
      { key: 'health_check_path', label: 'Smoke-test path', fallback: '/health' });
  }
  if (database === 'dynamodb') {
    out.push({ key: 'dynamodb_table_name', label: 'DynamoDB table name', fallback: '{app_id}-table' },
      { key: 'dynamodb_hash_key', label: 'DynamoDB partition key', fallback: 'PK (+ SK)' });
  }
  return out;
}

/** META value → detected_* (repo scan) → nothing. DynamoDB comes from the first detected table. */
export function wiringDefault(app: any, key: string): string | number | undefined {
  if (app?.[key] !== undefined && app?.[key] !== '') return app[key];
  const table = (app?.detected_dynamodb_tables || [])[0] || {};
  if (key === 'dynamodb_table_name') return table.name;
  if (key === 'dynamodb_hash_key') return table.hash_key;
  return app?.[`detected_${key}`];
}

/** Confirmed wiring values to persist (non-empty only; the port as a number). */
export function wiringPayload(app: any, edits: Record<string, string>, compute?: string, database?: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of wiringFields(compute, database)) {
    const raw = edits[f.key] ?? wiringDefault(app, f.key);
    const v = raw === undefined || raw === null ? '' : String(raw).trim();
    if (!v || v === String(app?.[f.key] ?? '')) continue;
    out[f.key] = f.key === 'container_port' ? Number(v) : v;
  }
  return out;
}
