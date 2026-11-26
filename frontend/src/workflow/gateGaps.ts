// Completeness-gate gaps surfaced on an approval gate's decision_summary
// ("completeness gate (design) missing: compliance, data_classification") that a
// reviewer can fill in right there. The values are saved on the app META and,
// on Retry, appended to the reviewer comment the re-run prompt receives.

type GapField = {
  label: string;
  kind: 'multi' | 'select' | 'text';
  options?: { label: string; value: string }[];
  placeholder?: string;
  description?: string;
};

export const GAP_FIELDS: Record<string, GapField> = {
  compliance: {
    label: 'Compliance',
    kind: 'multi',
    description: 'Frameworks the app must satisfy. Choose "None" if none apply.',
    options: ['None', 'HIPAA', 'PCI-DSS', 'SOX', 'GDPR', 'SOC2', 'ISO27001', 'FedRAMP'].map((v) => ({ label: v, value: v })),
  },
  data_classification: {
    label: 'Data Classification',
    kind: 'select',
    options: ['public', 'internal', 'confidential', 'restricted'].map((v) => ({ label: v[0].toUpperCase() + v.slice(1), value: v })),
  },
  rpo: { label: 'RPO', kind: 'text', placeholder: 'e.g. 4 hours' },
  rto: { label: 'RTO', kind: 'text', placeholder: 'e.g. 8 hours' },
  uptime_sla: { label: 'Uptime SLA', kind: 'text', placeholder: 'e.g. 99.9%' },
  target_region: { label: 'Target Region', kind: 'text', placeholder: 'us-east-1' },
};

const GAP_RE = /completeness gate \([\w-]+\) missing:\s*(.+)$/i;

/** Fillable fields named by the gate's completeness warnings (others — owners,
 *  machine_ids … — need the app editor, not a single input). */
export function completenessGaps(warnings: string[] | undefined): string[] {
  const out: string[] = [];
  for (const w of warnings || []) {
    const m = GAP_RE.exec(w);
    if (!m) continue;
    for (const f of m[1].split(',').map((s) => s.trim())) {
      if (GAP_FIELDS[f] && !out.includes(f)) out.push(f);
    }
  }
  return out;
}

export type GapValues = Record<string, string | string[]>;

/** META update for the filled gaps (compliance is a list; empty values skipped). */
export function gapPayload(values: GapValues): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const [k, v] of Object.entries(values)) {
    if (!GAP_FIELDS[k]) continue;
    if (Array.isArray(v)) {
      const list = v.map((s) => s.trim()).filter(Boolean);
      if (list.length) out[k] = list;
    } else if (typeof v === 'string' && v.trim()) {
      out[k] = v.trim();
    }
  }
  return out;
}

/** Sentence appended to the Retry comment so the re-run uses the new values. */
function gapComment(payload: Record<string, string | string[]>): string {
  const parts = Object.entries(payload).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(', ') : v}`);
  return parts.length ? `Previously missing inputs are now on the app record — use them: ${parts.join('; ')}.` : '';
}

/** Retry comment = reviewer text + the gap sentence. */
export function retryComment(text: string, payload: Record<string, string | string[]>): string {
  return [text.trim(), gapComment(payload)].filter(Boolean).join(' ');
}
