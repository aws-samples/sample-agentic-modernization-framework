import React, { useState, useEffect, useCallback } from 'react';

import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import Badge from '@cloudscape-design/components/badge';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Button from '@cloudscape-design/components/button';
import Modal from '@cloudscape-design/components/modal';
import Textarea from '@cloudscape-design/components/textarea';
import Alert from '@cloudscape-design/components/alert';
import Popover from '@cloudscape-design/components/popover';
import FileUpload from '@cloudscape-design/components/file-upload';
import Table from '@cloudscape-design/components/table';
import Select from '@cloudscape-design/components/select';
import Pagination from '@cloudscape-design/components/pagination';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Tabs from '@cloudscape-design/components/tabs';
import Multiselect from '@cloudscape-design/components/multiselect';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Link from '@cloudscape-design/components/link';
import Checkbox from '@cloudscape-design/components/checkbox';
import * as api from '../api/portfolio';
import * as workflowApi from '../api/workflow';
import { listTransformations } from '../api/workbench';
import { buildTransformCatalog, type LiveTransformation } from '../workflow/transformCatalog';
import { getAnalysisByType, getAnalysisHtmlUrl, getTransformationGaps } from '../api/analysis';
import { apiClient, apiErrorMessage } from '../api/client';
import ValidationResults from '../components/workflow/ValidationResults';
import { LoadErrorAlert } from '../components/LoadErrorAlert';
import RiskScorePanel from '../components/workflow/RiskScorePanel';
import { BUSINESS_QUESTIONS, TEAM_QUESTIONS, STATUS_OPTIONS } from '../workflow/readiness';
import {
  cicdOptionsFor,
  computeOptionsFor,
  databaseOptionsFor,
  transformsAllowedFor,
  validateAssessmentInputs,
  requiresAmi,
  requiresDbInstanceClass,
  dbInstanceClassesFor,
  type Opt,
  type ValidationError,
} from '../workflow/strategyOptions';
import { phaseStatusLabel, statusLabel } from '../workflow/status';
import { PHASES, getPhaseIndex, phaseOf } from '../workflow/phases';
import { GAP_FIELDS, completenessGaps, gapPayload, retryComment, type GapValues } from '../workflow/gateGaps';
import { wiringDefault, wiringFields, wiringPayload } from '../workflow/wiring';
import { cleanAtxDoc } from '../workflow/atxDoc';
import { DEMO_READ_ONLY, DEMO_READ_ONLY_MESSAGE } from '../demo';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useCapabilities } from '../CapabilitiesContext';
import { renderMermaidInto } from '../lib/mermaid';
import { DEFAULT_WORKFLOW_POLL_MS, consoleUrl, positiveOr, targetRegionOptions } from '../lib/aws';
import FailureReasonModal from '../components/workflow/FailureReasonModal';
import { SandboxedHtmlFrame } from '../components/SandboxedHtmlFrame';
import { useRole } from '../auth/AuthContext';
import { Role, permissionHint, requiredRoleForGate } from '../auth/roles';

// Resolve the assessment-approval inputs for an app, constrained to its 7R
// strategy. Used by both the modal (render + inline validation) and
// handleApprove (persist), so the values shown are exactly the values saved and
// a stale/heuristic default can never contradict the strategy.
function pickAllowed(options: Opt[], ...candidates: (string | null | undefined)[]): Opt {
  for (const c of candidates) {
    const hit = c ? options.find((o) => o.value === c) : undefined;
    if (hit) return hit;
  }
  return options[0];
}

function guessTargetDatabase(engine?: string): string {
  const e = (engine || '').toLowerCase();
  if (e.includes('mysql')) return 'aurora_mysql';
  if (e.includes('mongo')) return 'dynamodb'; // no DocumentDB target
  if (e.includes('dynamo')) return 'dynamodb';
  return 'aurora_postgresql';
}

function resolveAssessmentInputs(
  app: any,
  sel: {
    compute: Opt | null; database: Opt | null; cicd: Opt | null;
    account: string; vpcCidr: string;
    amiId: string; dbInstanceClass: Opt | null;
    transforms: Record<string, boolean>; recommended: string[];
  },
) {
  const strategy = (app?.migration_strategy || '').toLowerCase();
  const computeOptions = computeOptionsFor(strategy);
  const databaseOptions = databaseOptionsFor(strategy);
  const cicdOptions = cicdOptionsFor(strategy);
  const transformsAllowed = transformsAllowedFor(strategy);

  // No EKS option yet (no IaC module) — containerized and monolith apps alike default to Fargate.
  const compute = pickAllowed(computeOptions, sel.compute?.value, app?.target_compute, 'ecs_fargate');
  const database = pickAllowed(databaseOptions, sel.database?.value, app?.target_database, guessTargetDatabase(app?.database?.engine));
  const cicd = pickAllowed(cicdOptions, sel.cicd?.value, app?.cicd_target, 'codepipeline');

  const merged = { ...sel.recommended.reduce((acc: Record<string, boolean>, id) => ({ ...acc, [id]: true }), {}), ...sel.transforms };
  const transformationSteps = transformsAllowed ? Object.entries(merged).filter(([, v]) => v).map(([k]) => k) : [];

  const values = {
    target_compute: compute.value,
    target_database: database.value,
    cicd_target: cicd.value,
    transformation_steps: transformationSteps,
    target_account: (sel.account || app?.target_account || '').trim(),
    // Entered at the assessment gate — no silent default (validator requires it).
    vpc_cidr: (sel.vpcCidr || app?.vpc_cidr || '').trim(),
    // Conditional sizing inputs — always sent (empty when not applicable) so the
    // backend knows this is the assessment approval and enforces them.
    ami_id: requiresAmi(compute.value) ? (sel.amiId || app?.ami_id || '').trim() : '',
    db_instance_class: requiresDbInstanceClass(database.value)
      ? (sel.dbInstanceClass?.value || app?.db_instance_class || '') : '',
  };
  const errors: ValidationError[] = validateAssessmentInputs(strategy, values);
  const dbInstanceClassOptions = dbInstanceClassesFor(database.value);
  return { strategy, computeOptions, databaseOptions, cicdOptions, transformsAllowed, compute, database, cicd, values, errors,
    needsAmi: requiresAmi(compute.value), needsDbInstanceClass: requiresDbInstanceClass(database.value), dbInstanceClassOptions };
}

const TRANSFORMATION_UNIT_LABELS: Record<string, string> = {
  U1: 'Java 21 + Jakarta',
  U2: 'Dependency modernisation',
  U3: 'Observability uplift',
  U4: 'Invariant test harness',
  U5: 'Framework migration',
};

const STRATEGIES = [
  { value: 'rehost', label: 'Rehost (Lift & Shift)' },
  { value: 'replatform', label: 'Replatform (Containerize + Managed Services)' },
  { value: 'refactor', label: 'Refactor (Cloud-Native / Microservices)' },
  { value: 'repurchase', label: 'Repurchase (Replace with SaaS)' },
  { value: 'retain', label: 'Retain (Keep As-Is)' },
  { value: 'retire', label: 'Retire (Decommission)' },
  { value: 'relocate', label: 'Relocate (VMware Cloud on AWS)' },
];

// ATX transformation catalog: workflow/transformCatalog.ts (the single source, live-list aware).

/** Normalize canonical assessment gaps ({requirement, scope, reason, severity})
 *  — plus optionally any selected TD not in the live catalog — into a flat list. */
function normalizeGaps(
  rawGaps: any[],
  selectedTds?: string[],
  catalog?: Set<string>,
): { label: string; detail: string; severity: string }[] {
  const out = (rawGaps || []).map((g: any) =>
    typeof g === 'string'
      ? { label: g, detail: 'Code update with no ATX custom transformation available.', severity: 'medium' }
      : {
          label: [g.requirement, g.scope].filter(Boolean).join(' — '),
          detail: g.reason || 'No ATX custom transformation available.',
          severity: (g.severity || 'medium').toLowerCase(),
        },
  );
  if (selectedTds && catalog) {
    for (const td of selectedTds) {
      if (!catalog.has(td)) out.push({ label: td, detail: 'Recommended by analysis but not in the live ATX catalog yet.', severity: 'medium' });
    }
  }
  return out;
}

/** Red panel listing code-update requirements with no matching ATX custom
 *  transformation. Rendered at both assessment-review and design-input level. */
function TransformationGapsPanel({ gaps }: { gaps: { label: string; detail: string; severity?: string }[] }) {
  if (!gaps || gaps.length === 0) return null;
  return (
    <div style={{ border: '1px solid #d91515', borderRadius: 8, padding: 12, background: '#fdf3f3' }}>
      <Box variant="h3" color="text-status-error">⚠ Custom Transformations to Build ({gaps.length})</Box>
      <Box fontSize="body-s" color="text-status-error" margin={{ bottom: 'xs' }}>
        These code-update requirements have no matching ATX custom transformation. A migration engineer must author them and publish to the ATX custom catalog before they can run as transformation steps.
      </Box>
      <SpaceBetween size="xs">
        {gaps.map((g, i) => (
          <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <Badge color={g.severity === 'high' ? 'red' : 'grey'}>{(g.severity || 'medium').toUpperCase()}</Badge>
            <div>
              <Box color="text-status-error">{g.label}</Box>
              <Box fontSize="body-s" color="text-body-secondary">{g.detail}</Box>
            </div>
          </div>
        ))}
      </SpaceBetween>
    </div>
  );
}

const STEP_LABELS: Record<string, string> = {
  discovery_review: 'Review Discovery & Strategy',
  assessment_review: 'Review Assessment',
  design_review: 'Review Design Document',
  execution_review: 'Review Execution',
  validation_review: 'Review Validation Results',
  cutover_review: 'Cutover Go / No-Go',
  handover_review: 'Review Handover Package',
};

// ── Markdown Renderer with Mermaid support ────────────────────────────

const MermaidBlock: React.FC<{ code: string }> = ({ code }) => {
  const ref = React.useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    let active = true;
    const el = ref.current;
    renderMermaidInto(el, code, () => active);
    return () => { active = false; };
  }, [code]);
  return <div ref={ref} style={{ textAlign: 'center', margin: '12px 0' }} />;
};

const MarkdownRenderer: React.FC<{ content: string; onPatterns?: () => void }> = ({ content, onPatterns }) => {
  return (
    <div style={{ maxHeight: 600, overflow: 'auto', padding: '16px 20px', backgroundColor: '#fff', borderRadius: 6, border: '1px solid #e9ebed' }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => <h1 style={{ fontSize: 20, fontWeight: 700, borderBottom: '2px solid #0972d3', paddingBottom: 6, marginTop: 20 }}>{children}</h1>,
          h2: ({ children }) => <h2 style={{ fontSize: 17, fontWeight: 600, borderBottom: '1px solid #eee', paddingBottom: 4, marginTop: 16 }}>{children}</h2>,
          h3: ({ children }) => <h3 style={{ fontSize: 15, fontWeight: 600, marginTop: 12 }}>{children}</h3>,
          h4: ({ children }) => <h4 style={{ fontSize: 14, fontWeight: 600, marginTop: 10 }}>{children}</h4>,
          p: ({ children }) => <p style={{ margin: '6px 0', lineHeight: 1.6, fontSize: 13 }}>{children}</p>,
          ul: ({ children }) => <ul style={{ margin: '4px 0', paddingLeft: 20 }}>{children}</ul>,
          ol: ({ children }) => <ol style={{ margin: '4px 0', paddingLeft: 20 }}>{children}</ol>,
          li: ({ children }) => <li style={{ margin: '2px 0', fontSize: 13 }}>{children}</li>,
          table: ({ children }) => <table style={{ borderCollapse: 'collapse', width: '100%', margin: '8px 0', fontSize: 12 }}>{children}</table>,
          th: ({ children }) => <th style={{ border: '1px solid #ddd', padding: '6px 10px', backgroundColor: '#f8f9fa', fontWeight: 600, textAlign: 'left' }}>{children}</th>,
          td: ({ children }) => <td style={{ border: '1px solid #ddd', padding: '6px 10px' }}>{children}</td>,
          code: ({ className, children }) => {
            const lang = className?.replace('language-', '') || '';
            const codeStr = String(children).replace(/\n$/, '');
            if (lang === 'mermaid') return <MermaidBlock code={codeStr} />;
            if (className) {
              return <pre style={{ backgroundColor: '#f4f5f7', padding: 12, borderRadius: 4, overflow: 'auto', fontSize: 12, lineHeight: 1.5 }}><code>{children}</code></pre>;
            }
            return <code style={{ backgroundColor: '#f0f1f3', padding: '1px 5px', borderRadius: 3, fontSize: 12 }}>{children}</code>;
          },
          blockquote: ({ children }) => <blockquote style={{ borderLeft: '3px solid #0972d3', paddingLeft: 12, margin: '8px 0', color: '#5f6b7a' }}>{children}</blockquote>,
          hr: () => <hr style={{ border: 'none', borderTop: '1px solid #e9ebed', margin: '16px 0' }} />,
          strong: ({ children }) => <strong style={{ fontWeight: 600 }}>{children}</strong>,
          a: ({ href, children }) => {
            const target = href || '';
            if (/pattern/i.test(target) && onPatterns) {
              return <Link href="#assessment-recommendations" onFollow={(event) => { event.preventDefault(); onPatterns(); }}>{children}</Link>;
            }
            if (/^https?:\/\//i.test(target)) {
              return <Link href={target} external>{children}</Link>;
            }
            if (target.startsWith('#')) return <a href={target}>{children}</a>;
            return <span title="Included in the generated assessment evidence">{children}</span>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
};

// ── Assessment Report Tabs (fetches from DynamoDB) ────────────────────

type RecommendedTransformation = {
  td_name: string;
  order?: number;
  scope?: string;
  rationale?: string;
  build_context?: string;
  effort?: { range?: string; basis?: string };
};

type TransformationGap = {
  requirement: string;
  scope?: string;
  reason?: string;
  severity?: 'low' | 'medium' | 'high' | string;
  effort?: { range?: string; basis?: string };
};

type QualificationCriterion = {
  criterion: string;
  value?: string | number | boolean;
  threshold?: string;
  pass?: boolean;
};

/** Normalizes an assessment-summary `result` object into the canonical
 * transformations shape, falling back to the pre-amf-recommendations-effort
 * flat `transformation_steps` array when the app was assessed before that
 * schema existed (see the recommendations/effort design notes U4
 * regression finding: apps assessed before this schema existed only have the
 * old shape, and would otherwise show no Recommendations tab at all).
 * Old-shape entries carry no `effort` — the UI renders '—' for those,
 * honestly, rather than inventing a number for data that predates
 * estimation. */
function normalizeTransformations(result: any): { recommended: RecommendedTransformation[]; gaps: TransformationGap[] } {
  const canonical = result?.transformations;
  if (canonical && (Array.isArray(canonical.recommended) || Array.isArray(canonical.gaps))) {
    return { recommended: canonical.recommended || [], gaps: canonical.gaps || [] };
  }
  const legacySteps = Array.isArray(result?.transformation_steps) ? result.transformation_steps : [];
  return {
    recommended: legacySteps.map((step: any, i: number) => ({
      td_name: step?.td_name || String(step),
      order: step?.order ?? i + 1,
      scope: step?.description || undefined,
      rationale: undefined,
      effort: undefined,
    })),
    gaps: [],
  };
}

const severityBadgeColor = (severity?: string) =>
  severity === 'high' ? 'red' : severity === 'medium' ? 'blue' : 'grey';

const AssessmentRecommendations: React.FC<{ appId: string }> = ({ appId }) => {
  const [loading, setLoading] = useState(true);
  const [recommended, setRecommended] = useState<RecommendedTransformation[]>([]);
  const [gaps, setGaps] = useState<TransformationGap[]>([]);
  const [criteria, setCriteria] = useState<QualificationCriterion[]>([]);
  const [error, setError] = useState<string | null>(null);

  const fetchRecommendations = useCallback(async () => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    try {
      const resp = await getAnalysisByType(appId, 'assessment-summary');
      const items = Array.isArray(resp) ? resp : (resp as any)?.data || [];
      const item = items[0];
      const result = item?.data?.result || item?.result || {};
      if (!cancelled) {
        const norm = normalizeTransformations(result);
        setRecommended(norm.recommended);
        setGaps(norm.gaps);
        setCriteria(result?.qualification_criteria || []);
      }
    } catch (exc) {
      if (!cancelled) setError(apiErrorMessage(exc));
    } finally {
      if (!cancelled) setLoading(false);
    }
    return () => { cancelled = true; };
  }, [appId]);

  useEffect(() => {
    let cleanup: (() => void) | undefined;
    fetchRecommendations().then((fn) => { cleanup = fn; });
    return () => { cleanup?.(); };
  }, [fetchRecommendations]);

  if (loading) return <StatusIndicator type="loading">Loading assessment recommendations...</StatusIndicator>;
  if (error) return <LoadErrorAlert message={error} onRetry={fetchRecommendations} />;

  const automatedCount = recommended.length;
  const gapCount = gaps.length;
  const totalItems = automatedCount + gapCount;
  const effortRanges = [...recommended, ...gaps]
    .map((item) => item.effort?.range)
    .filter((range): range is string => Boolean(range));

  return (
    <SpaceBetween size="l">
      <Alert type="info" header="Assessment translated into an executable plan">
        {totalItems === 0
          ? 'No modernization findings require action for this application yet — run or re-run Assessment to populate this view.'
          : `${automatedCount} ATX-automatable ${automatedCount === 1 ? 'step' : 'steps'} and ${gapCount} custom-authoring ${gapCount === 1 ? 'gap' : 'gaps'} identified. Effort estimates are preliminary — refined once the transformation is scoped in detail.`}
      </Alert>
      <ColumnLayout columns={4} variant="text-grid">
        <div><Box variant="awsui-key-label">Total findings</Box><Box variant="h2">{totalItems}</Box></div>
        <div><Box variant="awsui-key-label">ATX-automated</Box><Box variant="h2">{automatedCount}</Box></div>
        <div><Box variant="awsui-key-label">Custom-authoring gaps</Box><Box variant="h2">{gapCount}</Box></div>
        <div><Box variant="awsui-key-label">Effort range (per item)</Box><Box variant="h2">
          {effortRanges.length > 0 ? effortRanges.join(', ') : '—'}
        </Box></div>
      </ColumnLayout>
      <div id="assessment-recommendations">
        <Header variant="h3" description="Recommended ATX-automated transformations, ordered by execution sequence.">Recommended transformations</Header>
        {recommended.length === 0 ? (
          <Box color="text-body-secondary">No ATX-automatable transformations identified for this application.</Box>
        ) : (
          <Table
            variant="embedded"
            wrapLines
            items={recommended}
            columnDefinitions={[
              { id: 'order', header: 'Order', width: 70, cell: (item: RecommendedTransformation) => item.order ?? '—' },
              { id: 'td_name', header: 'Transformation', width: 200, cell: (item: RecommendedTransformation) => <Badge color="blue">{item.td_name}</Badge> },
              { id: 'scope', header: 'Scope', cell: (item: RecommendedTransformation) => item.scope || '—' },
              { id: 'rationale', header: 'Rationale', cell: (item: RecommendedTransformation) => item.rationale || '—' },
              { id: 'effort', header: 'Effort', width: 120, cell: (item: RecommendedTransformation) => item.effort?.range || '—' },
            ]}
          />
        )}
      </div>
      <div>
        <Header variant="h3" description="Findings with no matching ATX catalog transformation — require a migration engineer to author a custom definition.">Custom-authoring gaps</Header>
        {gaps.length === 0 ? (
          <Box color="text-body-secondary">No gaps identified — every modernization need has a matching ATX transformation.</Box>
        ) : (
          <Table
            variant="embedded"
            wrapLines
            items={gaps}
            columnDefinitions={[
              { id: 'requirement', header: 'Requirement', cell: (item: TransformationGap) => item.requirement },
              { id: 'scope', header: 'Scope', cell: (item: TransformationGap) => item.scope || '—' },
              { id: 'severity', header: 'Severity', width: 100, cell: (item: TransformationGap) => <Badge color={severityBadgeColor(item.severity)}>{item.severity || 'unknown'}</Badge> },
              { id: 'reason', header: 'Reason', cell: (item: TransformationGap) => item.reason || '—' },
              { id: 'effort', header: 'Effort', width: 120, cell: (item: TransformationGap) => item.effort?.range || '—' },
            ]}
          />
        )}
      </div>
      {criteria.length > 0 && (
        <div>
          <Header variant="h3" description="Inspectable checks behind the assessment's candidacy determination.">Qualification criteria</Header>
          <Table
            variant="embedded"
            wrapLines
            items={criteria}
            columnDefinitions={[
              { id: 'criterion', header: 'Criterion', cell: (item: QualificationCriterion) => item.criterion },
              { id: 'value', header: 'Value', cell: (item: QualificationCriterion) => String(item.value ?? '—') },
              { id: 'threshold', header: 'Threshold', width: 120, cell: (item: QualificationCriterion) => item.threshold || '—' },
              { id: 'pass', header: 'Pass', width: 90, cell: (item: QualificationCriterion) => item.pass === undefined ? '—' : <StatusIndicator type={item.pass ? 'success' : 'error'}>{item.pass ? 'Pass' : 'Fail'}</StatusIndicator> },
            ]}
          />
        </div>
      )}
    </SpaceBetween>
  );
};

const AssessmentReportTabs: React.FC<{ appId: string }> = ({ appId }) => {
  const [reports, setReports] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);
  const [hasRecommendations, setHasRecommendations] = useState(false);
  const [activeTabId, setActiveTabId] = useState('code');

  useEffect(() => {
    setActiveTabId('code');
    (async () => {
      setLoading(true);
      const tabs: Record<string, any> = {};
      for (const type of ['comprehensive-codebase-analysis', 'modernization-readiness-analysis', 'agentic-readiness-analysis']) {
        try {
          const resp = await apiClient.get(`/analysis/${appId}/${type}`);
          const items = Array.isArray(resp) ? resp : (resp as any)?.data || [];
          if (items.length > 0) {
            const item = items[0];
            const itemData = item.data || {};
            if (itemData.report_md || Object.keys(itemData).length > 0) tabs[type] = item;
          }
        } catch { /* skip */ }
      }
      // Recommendations tab shows whenever this app has real transformation or
      // qualification data (new OR old schema) — never gated to a specific
      // app_id (see the recommendations/effort design notes R1).
      let recPresent = false;
      try {
        const resp = await getAnalysisByType(appId, 'assessment-summary');
        const items = Array.isArray(resp) ? resp : (resp as any)?.data || [];
        const item = items[0];
        const result = item?.data?.result || item?.result || {};
        const norm = normalizeTransformations(result);
        recPresent = Boolean(
          norm.recommended.length > 0 ||
          norm.gaps.length > 0 ||
          (result?.qualification_criteria?.length ?? 0) > 0
        );
      } catch { /* skip */ }
      setReports(tabs);
      setHasRecommendations(recPresent);
      setActiveTabId(recPresent ? 'recommendations' : 'code');
      setLoading(false);
    })();
  }, [appId]);

  if (loading) return <StatusIndicator type="loading">Loading reports...</StatusIndicator>;

  const openRecommendations = () => setActiveTabId('recommendations');
  const availableTabs = [
    hasRecommendations && {
      id: 'recommendations',
      label: 'Recommendations & Effort',
      content: <AssessmentRecommendations appId={appId} />,
    },
    reports['comprehensive-codebase-analysis'] && {
      id: 'code',
      label: 'Comprehensive Analysis',
      content: <ReportMarkdown data={reports['comprehensive-codebase-analysis']} onPatterns={openRecommendations} />,
    },
    reports['modernization-readiness-analysis'] && {
      id: 'modernization',
      label: 'Modernization',
      content: <ReportMarkdown data={reports['modernization-readiness-analysis']} onPatterns={openRecommendations} />,
    },
    reports['agentic-readiness-analysis'] && {
      id: 'agentic',
      label: 'Agentic',
      content: <ReportMarkdown data={reports['agentic-readiness-analysis']} onPatterns={openRecommendations} />,
    },
  ].filter(Boolean) as any[];

  if (availableTabs.length === 0) return <Box color="text-body-secondary">No assessment reports available yet</Box>;
  const resolvedActiveTabId = availableTabs.some(tab => tab.id === activeTabId) ? activeTabId : availableTabs[0].id;

  return <Tabs activeTabId={resolvedActiveTabId} onChange={({ detail }) => setActiveTabId(detail.activeTabId)} tabs={availableTabs} />;
};

// Render a parsed JSON "report" (e.g. execution-database) as a professional,
// readable block instead of dumping the raw stringified object into markdown.
// Flat scalars → one Container with a 2-column key/value grid; nested objects →
// a sub-Container per nested key with a nested grid. Nulls render as "—".
const StructuredJsonReport: React.FC<{ obj: Record<string, any>; analysisType?: string }> = ({ obj, analysisType }) => {
  const title = (k: string) =>
    k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  const scalar = (v: any) =>
    v === null || v === undefined || v === ''
      ? <Box color="text-body-secondary">—</Box>
      : typeof v === 'boolean'
        ? <Badge color={v ? 'green' : 'grey'}>{v ? 'Yes' : 'No'}</Badge>
        : Array.isArray(v)
          ? <SpaceBetween size="xxs">{v.map((x, i) => <Box key={i} fontSize="body-s">{typeof x === 'object' ? JSON.stringify(x) : String(x)}</Box>)}</SpaceBetween>
          : <Box fontSize="body-s">{String(v)}</Box>;

  // Pull a status/determination summary to the top when present.
  const statusLabel = String(obj.status ?? obj.determination_status ?? '').toLowerCase();
  const statusColor: 'green' | 'blue' | 'red' | 'grey' =
    statusLabel.includes('not_required') || statusLabel.includes('skipped') ? 'grey'
      : statusLabel.includes('required') || statusLabel.includes('pending') ? 'blue'
      : statusLabel.includes('error') || statusLabel.includes('blocked') ? 'red' : 'green';

  const scalars: [string, any][] = [];
  const nested: [string, Record<string, any>][] = [];
  const prose: [string, string][] = [];
  for (const [k, v] of Object.entries(obj)) {
    if (k === 'status' || k === 'determination') continue; // pulled out above
    if (v && typeof v === 'object' && !Array.isArray(v)) nested.push([k, v as Record<string, any>]);
    else if (typeof v === 'string' && v.length > 160) prose.push([k, v]);
    else scalars.push([k, v]);
  }

  return (
    <SpaceBetween size="l">
      {(obj.status || obj.determination) && (
        <Container header={<Header variant="h3">Summary</Header>}>
          <SpaceBetween size="s">
            {obj.status && (
              <div>
                <Box variant="awsui-key-label">Status</Box>
                <Badge color={statusColor}>{title(String(obj.status))}</Badge>
              </div>
            )}
            {obj.determination && (
              <div>
                <Box variant="awsui-key-label">Determination</Box>
                <Box>{String(obj.determination)}</Box>
              </div>
            )}
          </SpaceBetween>
        </Container>
      )}

      {scalars.length > 0 && (
        <Container header={<Header variant="h3">{analysisType === 'execution-database' ? 'Migration Plan' : 'Details'}</Header>}>
          <ColumnLayout columns={2} variant="text-grid">
            {scalars.map(([k, v]) => (
              <div key={k}>
                <Box variant="awsui-key-label">{title(k)}</Box>
                {scalar(v)}
              </div>
            ))}
          </ColumnLayout>
        </Container>
      )}

      {prose.map(([k, v]) => (
        <Container key={k} header={<Header variant="h3">{title(k)}</Header>}>
          <Box>{v}</Box>
        </Container>
      ))}

      {nested.map(([k, child]) => (
        <Container key={k} header={<Header variant="h3">{title(k)}</Header>}>
          <ColumnLayout columns={1} variant="text-grid">
            {Object.entries(child).map(([ck, cv]) => (
              <div key={ck}>
                <Box variant="awsui-key-label">{title(ck)}</Box>
                {scalar(cv)}
              </div>
            ))}
          </ColumnLayout>
        </Container>
      ))}
    </SpaceBetween>
  );
};

const ReportMarkdown: React.FC<{ data: any; onPatterns?: () => void }> = ({ data, onPatterns }) => {
  const itemData = data?.data || {};
  const content = itemData.report_md || '';

  // Some reports (e.g. execution-database) stash a JSON object into `report_md`
  // as a string. Rendering that as markdown dumps raw JSON into the UI; parse it
  // and render a professional structured block instead.
  const trimmed = content.trim();
  if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object') {
        return <StructuredJsonReport obj={parsed} analysisType={data?.analysis_type} />;
      }
    } catch { /* fall through to markdown */ }
  }
  // Also fall back to `result` when report_md is empty but result is an object.
  if (!content && data?.result && typeof data.result === 'object' && !Array.isArray(data.result)) {
    return <StructuredJsonReport obj={data.result} analysisType={data?.analysis_type} />;
  }

  if (!content) {
    // Combine individual keys into markdown
    const sections: string[] = [];
    const keyLabels: Record<string, string> = {
      project_overview: 'Project Overview',
      architecture_overview: 'Architecture Overview',
      architecture_components: 'Architecture Components',
      technical_debt_report: 'Technical Debt',
      code_metrics: 'Code Metrics',
      complexity_analysis: 'Complexity Analysis',
      security_analysis: 'Security Analysis',
      migration_order: 'Migration Order',
      remediation_plan: 'Remediation Plan',
      behavior_business_logic: 'Business Logic',
    };
    for (const [key, label] of Object.entries(keyLabels)) {
      if (itemData[key]) {
        sections.push(`## ${label}\n\n${cleanAtxDoc(itemData[key])}`);
      }
    }
    for (const [key, value] of Object.entries(itemData)) {
      if (!keyLabels[key] && typeof value === 'string' && value.length > 20) {
        sections.push(`## ${key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}\n\n${cleanAtxDoc(value)}`);
      }
    }
    if (sections.length === 0) return <Box color="text-body-secondary">Report content not available</Box>;
    return <MarkdownRenderer content={sections.join('\n\n---\n\n')} onPatterns={onPatterns} />;
  }

  // Detect HTML content
  if (content.trim().startsWith('<!DOCTYPE') || content.trim().startsWith('<html') || content.trim().startsWith('<div')) {
    return (
      <div style={{ maxHeight: 600, overflow: 'auto', border: '1px solid #e9ebed', borderRadius: 6 }}>
        <SandboxedHtmlFrame html={content} title="Report" />
      </div>
    );
  }

  // Markdown content
  return <MarkdownRenderer content={content} onPatterns={onPatterns} />;
};

// ── Migration Plan Content ─────────────────────────────────────────────

const MigrationPlanContent: React.FC<{ appId: string }> = ({ appId }) => {
  const caps = useCapabilities();
  const [planMd, setPlanMd] = useState('');
  const [_defaults, _setDefaults] = useState<any>(null);
  const [inputs, setInputs] = useState<any>({});
  const [validation, setValidation] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [tdOptions, setTdOptions] = useState<{ value: string; label: string }[]>([]);
  // Strategy drives which compute / DB / CI-CD / transform options are shown.
  const [appStrategy, setAppStrategy] = useState<string>('');
  const [tdError, setTdError] = useState<string>('');

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const [planResp, defaultsResp, inputsResp] = await Promise.all([
          apiClient.get(`/workflow/migration-plan/${appId}`).catch(() => null),
          apiClient.get(`/workflow/migration-plan/${appId}/defaults`).catch(() => null),
          apiClient.get(`/portfolio/apps/${appId}`).catch(() => null),
        ]);
        const planData = (planResp as any)?.data || planResp;
        setPlanMd(planData?.plan_md || '');
        
        // Load saved inputs from app META (engineer_inputs field), fall back to defaults
        const appData = (inputsResp as any)?.data || inputsResp || {};
        setAppStrategy(appData?.migration_strategy || '');
        const savedInputs = appData?.engineer_inputs || null;
        const defs = (defaultsResp as any)?.data || defaultsResp || {};
        _setDefaults(defs);
        
        if (savedInputs && Object.keys(savedInputs).length > 0) {
          setInputs(savedInputs);
        } else {
          setInputs(defs);
        }
      } catch {}
      setLoading(false);
    })();
  }, [appId]);

  // Load the live ATX transformation list (atx custom def list) so engineers
  // see the full, account-available set rather than a hardcoded subset.
  useEffect(() => {
    (async () => {
      try {
        const resp: any = await listTransformations();
        const items = Array.isArray(resp) ? resp : (resp?.data ?? []);
        if (Array.isArray(items) && items.length > 0) {
          setTdOptions(items.map((t: any) => ({
            value: t.name,
            label: t.display_name ? `${t.name} — ${t.display_name}` : t.name,
          })));
          setTdError('');
        } else {
          // ATX returned an error/empty payload — fall back to the static list.
          setTdError(resp?.message || resp?.data?.message || 'ATX transformation list unavailable — showing defaults.');
        }
      } catch (err: any) {
        setTdError(err?.message || 'Could not reach ATX — showing default transformations.');
      }
    })();
  }, [appId]);

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      const resp = await apiClient.post(`/workflow/migration-plan/${appId}/submit`, { app_id: appId, ...inputs });
      const result = (resp as any);
      setValidation(result?.validation || result);
      if (result?.validation?.valid || result?.valid) {
        // Inputs saved successfully
      }
    } catch {}
    setSubmitting(false);
  };

  if (loading) return <StatusIndicator type="loading">Loading migration plan...</StatusIndicator>;

  // Dropdown options are strategy-driven (single source: workflow/strategyOptions.ts).
  // The chosen 7R strategy constrains what the user can pick so target_compute /
  // target_database / cicd_target never contradict strategy. The backend validator
  // on PUT /portfolio/apps/{id} rejects the same combinations if a client bypasses.
  const COMPUTE_OPTIONS = computeOptionsFor(appStrategy);
  const DB_OPTIONS = databaseOptionsFor(appStrategy);
  const CICD_OPTIONS = cicdOptionsFor(appStrategy);
  const transformsAllowed = transformsAllowedFor(appStrategy);
  const DEPLOY_OPTIONS = [
    { value: 'blue_green', label: 'Blue/Green (Zero Downtime)' },
    { value: 'rolling', label: 'Rolling Update' },
    { value: 'canary', label: 'Canary Release' },
  ];
  const REGION_OPTIONS = targetRegionOptions(caps);

  // Fallback used only when the live ATX list cannot be fetched: the known catalog.
  const TRANSFORM_TD_FALLBACK = buildTransformCatalog({}).flatMap((c) => c.items)
    .map((i) => ({ value: i.id, label: i.id }));
  // Prefer the live ATX list; fall back to the static set if unavailable.
  const TRANSFORM_TD_OPTIONS = tdOptions.length > 0 ? tdOptions : TRANSFORM_TD_FALLBACK;
  const TESTING_OPTIONS = [
    { value: 'unit', label: 'Unit Tests' },
    { value: 'integration', label: 'Integration Tests' },
    { value: 'smoke', label: 'Smoke Tests' },
    { value: 'load', label: 'Load/Performance Tests' },
    { value: 'security', label: 'Security Scan (SAST/DAST)' },
    { value: 'e2e', label: 'End-to-End Tests' },
  ];

  return (
    <Tabs tabs={[
      {
        id: 'plan',
        label: 'Migration Plan',
        content: planMd ? <MarkdownRenderer content={planMd} /> : <Box color="text-body-secondary">Plan not generated yet</Box>,
      },
      {
        id: 'inputs',
        label: 'Additional Input',
        content: (
          <SpaceBetween size="m">
            <Alert type="info">Select your preferred AWS services and configuration. Choices are validated against AWS best practices.</Alert>

            <Header variant="h3">Infrastructure</Header>
            <ColumnLayout columns={2}>
              <FormField label="AWS Account">
                <Select expandToViewport selectedOption={{ value: inputs.account || 'create_new', label: inputs.account === 'existing' ? 'Use Existing' : 'Create New' }} onChange={({ detail }) => setInputs({ ...inputs, account: detail.selectedOption.value })} options={[{ value: 'create_new', label: 'Create New Account' }, { value: 'existing', label: 'Use Existing Account' }]} />
              </FormField>
              {inputs.account === 'existing' && (
                <FormField label="Account ID"><input type="text" placeholder="123456789012" value={inputs.account_id || ''} onChange={(e) => setInputs({ ...inputs, account_id: e.target.value })} style={{ width: '100%', padding: '6px 10px', border: '1px solid #d5dbdb', borderRadius: 4 }} /></FormField>
              )}
              <FormField label="Target Region(s)" description="Primary + optional DR region">
                <Multiselect expandToViewport selectedOptions={REGION_OPTIONS.filter(o => (inputs.target_regions || []).includes(o.value))} onChange={({ detail }) => setInputs({ ...inputs, target_regions: detail.selectedOptions.map((o: any) => o.value) })} options={REGION_OPTIONS} placeholder="Select regions..." />
              </FormField>
              <FormField label="VPC">
                <Select expandToViewport selectedOption={{ value: inputs.vpc || 'create_new', label: inputs.vpc === 'existing' ? 'Use Existing' : 'Create New' }} onChange={({ detail }) => setInputs({ ...inputs, vpc: detail.selectedOption.value })} options={[{ value: 'create_new', label: 'Create New VPC' }, { value: 'existing', label: 'Use Existing VPC' }]} />
              </FormField>
              {inputs.vpc === 'existing' && (
                <FormField label="VPC ID"><input type="text" placeholder="vpc-0123456789abcdef0" value={inputs.vpc_id || ''} onChange={(e) => setInputs({ ...inputs, vpc_id: e.target.value })} style={{ width: '100%', padding: '6px 10px', border: '1px solid #d5dbdb', borderRadius: 4 }} /></FormField>
              )}
              <FormField label="DNS (Route53)">
                <Select expandToViewport selectedOption={{ value: inputs.dns_route53 || 'yes_new', label: inputs.dns_route53 === 'no' ? 'No' : inputs.dns_route53 === 'yes_existing' ? 'Yes (Existing Zone)' : 'Yes (New Zone)' }} onChange={({ detail }) => setInputs({ ...inputs, dns_route53: detail.selectedOption.value })} options={[{ value: 'yes_new', label: 'Yes — Create New Hosted Zone' }, { value: 'yes_existing', label: 'Yes — Use Existing Hosted Zone' }, { value: 'no', label: 'No DNS Required' }]} />
              </FormField>
              <FormField label="Compute Service">
                <Select expandToViewport selectedOption={COMPUTE_OPTIONS.find(o => o.value === inputs.compute_service) || COMPUTE_OPTIONS[0]} onChange={({ detail }) => setInputs({ ...inputs, compute_service: detail.selectedOption.value })} options={COMPUTE_OPTIONS} />
              </FormField>
              <FormField label="Target Database">
                <Select expandToViewport selectedOption={DB_OPTIONS.find(o => o.value === inputs.target_database) || DB_OPTIONS[0]} onChange={({ detail }) => setInputs({ ...inputs, target_database: detail.selectedOption.value })} options={DB_OPTIONS} />
              </FormField>
              <FormField label="DB Backup Retention">
                <Select expandToViewport selectedOption={{ value: String(inputs.backup_retention_days || 14), label: `${inputs.backup_retention_days || 14} days` }} onChange={({ detail }) => setInputs({ ...inputs, backup_retention_days: parseInt(detail.selectedOption.value || '14') })} options={[{ value: '7', label: '7 days' }, { value: '14', label: '14 days' }, { value: '30', label: '30 days' }, { value: '35', label: '35 days' }]} />
              </FormField>
              <FormField label="Multi-AZ (High Availability)">
                <Select expandToViewport selectedOption={{ value: inputs.multi_az || 'yes', label: inputs.multi_az === 'no' ? 'No' : 'Yes' }} onChange={({ detail }) => setInputs({ ...inputs, multi_az: detail.selectedOption.value })} options={[{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }]} />
              </FormField>
              <FormField label="Encryption">
                <Select expandToViewport selectedOption={{ value: inputs.encryption || 'aws_managed', label: inputs.encryption === 'cmk' ? 'Customer Managed Key (CMK)' : 'AWS Managed' }} onChange={({ detail }) => setInputs({ ...inputs, encryption: detail.selectedOption.value })} options={[{ value: 'aws_managed', label: 'AWS Managed Key' }, { value: 'cmk', label: 'Customer Managed Key (CMK)' }]} />
              </FormField>
            </ColumnLayout>

            <Header variant="h3">Deployment</Header>
            <ColumnLayout columns={2}>
              <FormField label="CI/CD Provider">
                <Select expandToViewport disabled={CICD_OPTIONS.length <= 1} selectedOption={CICD_OPTIONS.find(o => o.value === inputs.cicd_provider) || CICD_OPTIONS[0]} onChange={({ detail }) => setInputs({ ...inputs, cicd_provider: detail.selectedOption.value })} options={CICD_OPTIONS} />
              </FormField>
              <FormField label="Deployment Strategy">
                <Select expandToViewport selectedOption={DEPLOY_OPTIONS.find(o => o.value === inputs.deployment_strategy) || DEPLOY_OPTIONS[0]} onChange={({ detail }) => setInputs({ ...inputs, deployment_strategy: detail.selectedOption.value })} options={DEPLOY_OPTIONS} />
              </FormField>
            </ColumnLayout>

            <Header variant="h3">Additional Transformation Steps (ATX — in execution order)</Header>
            {!transformsAllowed && (
              <Alert type="info">
                Code transformations do not apply to strategy=<b>{appStrategy || 'unknown'}</b> — this app
                migrates AS-IS. Transformation picker is disabled.
              </Alert>
            )}
            {transformsAllowed && <Box fontSize="body-s" color="text-body-secondary">Select additional pre/post steps beyond the auto-generated plan. Add build context for each (e.g. targetJDKVersion=17).</Box>}
            {tdError && <Alert type="warning">{tdError}</Alert>}
            <Multiselect
              expandToViewport
              disabled={!transformsAllowed}
              selectedOptions={transformsAllowed ? TRANSFORM_TD_OPTIONS.filter(o => (inputs.transformation_steps || []).includes(o.value)) : []}
              onChange={({ detail }) => setInputs({ ...inputs, transformation_steps: detail.selectedOptions.map((o: any) => o.value) })}
              options={TRANSFORM_TD_OPTIONS}
              placeholder={transformsAllowed ? 'Select transformation definitions (in order)...' : 'Not applicable for this strategy'}
            />
            {/* Build context for each selected step */}
            {(inputs.transformation_steps || []).length > 0 && (
              <SpaceBetween size="xs">
                <Box variant="awsui-key-label" margin={{ top: 's' }}>Build Context per Step</Box>
                {(inputs.transformation_steps || []).map((td: string) => (
                  <div key={td} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Badge color="blue">{td}</Badge>
                    <input
                      type="text"
                      placeholder="e.g. targetJDKVersion=17"
                      value={(inputs.transformation_build_context || {})[td] || ''}
                      onChange={(e) => setInputs({
                        ...inputs,
                        transformation_build_context: {
                          ...(inputs.transformation_build_context || {}),
                          [td]: e.target.value,
                        },
                      })}
                      style={{ flex: 1, padding: '6px 10px', border: '1px solid #d5dbdb', borderRadius: 4, fontSize: 13 }}
                    />
                  </div>
                ))}
              </SpaceBetween>
            )}

            {/* Custom-transformation gaps: analysis-recommended code updates with
                no matching ATX definition. Shown in red so engineers author them
                and add them to the ATX custom catalog. */}
            <TransformationGapsPanel
              gaps={normalizeGaps(
                inputs.transformation_gaps || [],
                inputs.transformation_steps || [],
                new Set(TRANSFORM_TD_OPTIONS.map((o: any) => o.value)),
              )}
            />

            <Header variant="h3">Testing Strategy</Header>
            <Multiselect
              expandToViewport
              selectedOptions={TESTING_OPTIONS.filter(o => (inputs.testing_strategy || []).includes(o.value))}
              onChange={({ detail }) => setInputs({ ...inputs, testing_strategy: detail.selectedOptions.map((o: any) => o.value) })}
              options={TESTING_OPTIONS}
              placeholder="Select testing types..."
            />

            {/* Validation Results */}
            {validation && (
              <SpaceBetween size="s">
                {validation.valid && <Alert type="success">All inputs validated. Ready to approve.</Alert>}
                {validation.errors?.map((e: any, i: number) => (
                  <Alert key={i} type="error" header={e.field}>{e.message}<br/><strong>Suggestion:</strong> {e.suggestion}</Alert>
                ))}
                {validation.warnings?.map((w: any, i: number) => (
                  <Alert key={i} type="warning" header={w.field}>{w.message}<br/><strong>Suggestion:</strong> {w.suggestion}</Alert>
                ))}
              </SpaceBetween>
            )}

            <Box float="right">
              <Button variant="primary" onClick={handleSubmit} loading={submitting}>Save Inputs</Button>
            </Box>
          </SpaceBetween>
        ),
      },
    ]} />
  );
};

// ── Wave Plan Report Component ─────────────────────────────────────────

const WavePlanReport: React.FC<{ appId: string }> = ({ appId }) => {
  const [wavePlan, setWavePlan] = useState<any>(null);
  const [apps, setApps] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const [wpResp, appsResp] = await Promise.all([
          apiClient.get('/waves/plan').catch(() => null),
          apiClient.get('/portfolio/apps').catch(() => null),
        ]);
        const wpData = (wpResp as any)?.data || wpResp;
        setWavePlan(wpData);
        const appsData = (appsResp as any)?.data || appsResp;
        setApps(Array.isArray(appsData) ? appsData : []);
      } catch { setWavePlan(null); }
      setLoading(false);
    })();
  }, [appId]);

  if (loading) return <StatusIndicator type="loading">Loading wave plan...</StatusIndicator>;
  if (!wavePlan || !wavePlan.waves) return <Box color="text-body-secondary">No wave plan generated yet. Complete assessment and migration plan first.</Box>;

  const waves = wavePlan.waves || [];
  const summary = wavePlan.summary || {};
  const conflicts = wavePlan.conflicts || [];

  // Find this app's wave
  const appWave = waves.find((w: any) => w.apps?.some((a: any) => a.app_id === appId));
  const appEntry = appWave?.apps?.find((a: any) => a.app_id === appId);

  // Get full app data for enrichment
  const getAppInfo = (id: string) => apps.find((a: any) => a.app_id === id) || {};

  return (
    <SpaceBetween size="m">
      {/* This app's wave assignment */}
      {appWave && (
        <Container header={<Header variant="h3">Wave Assignment for {appId}</Header>}>
          <ColumnLayout columns={4} variant="text-grid">
            <div><Box variant="awsui-key-label">Wave</Box><Box variant="awsui-value-large">{appWave.wave_number}</Box></div>
            <div><Box variant="awsui-key-label">Wave Label</Box><Box>{appWave.label || `Wave ${appWave.wave_number}`}</Box></div>
            <div><Box variant="awsui-key-label">Apps in Wave</Box><Box>{appWave.apps?.length || 0}</Box></div>
            <div><Box variant="awsui-key-label">Strategy</Box><Badge color="blue">{appEntry?.strategy || getAppInfo(appId)?.migration_strategy || '—'}</Badge></div>
          </ColumnLayout>
        </Container>
      )}

      {/* Summary */}
      <Container header={<Header variant="h3">Wave Plan Summary</Header>}>
        <ColumnLayout columns={4} variant="text-grid">
          <div><Box variant="awsui-key-label">Total Waves</Box><Box>{summary.total_waves || waves.length}</Box></div>
          <div><Box variant="awsui-key-label">Total Applications</Box><Box>{summary.total_apps || waves.reduce((s: number, w: any) => s + (w.apps?.length || 0), 0)}</Box></div>
          <div><Box variant="awsui-key-label">Conflicts</Box><Box>{conflicts.length > 0 ? <Badge color="red">{conflicts.length}</Badge> : '0'}</Box></div>
          <div><Box variant="awsui-key-label">Generated</Box><Box fontSize="body-s">{wavePlan.generated_at ? new Date(wavePlan.generated_at).toLocaleString() : '—'}</Box></div>
        </ColumnLayout>
      </Container>

      {/* All Waves */}
      {waves.map((wave: any) => {
        const isCurrentWave = wave.wave_number === appWave?.wave_number;
        return (
          <Container key={wave.wave_number} header={<Header variant="h3">
            {wave.label || `Wave ${wave.wave_number}`}
            {isCurrentWave && <Badge color="blue"> ← This App</Badge>}
          </Header>}>
            <Table
              columnDefinitions={[
                { id: 'app', header: 'Application', cell: (a: any) => {
                  const info = getAppInfo(a.app_id);
                  return <Box fontWeight={a.app_id === appId ? 'bold' : 'normal'}>{info.app_name || a.app_name || a.app_id}{a.app_id === appId ? ' ★' : ''}</Box>;
                }, width: 180 },
                { id: 'bu', header: 'BU', cell: (a: any) => <Badge>{getAppInfo(a.app_id)?.business_unit || '—'}</Badge>, width: 80 },
                { id: 'strategy', header: 'Strategy', cell: (a: any) => <Badge color="blue">{a.strategy || getAppInfo(a.app_id)?.migration_strategy || '—'}</Badge>, width: 100 },
                { id: 'tier', header: 'Tier', cell: (a: any) => {
                  const tier = a.tier || getAppInfo(a.app_id)?.tier || '';
                  return <Badge color={tier === 'tier_1' ? 'red' : tier === 'tier_2' ? 'blue' : 'grey'}>{tier.replace('_', ' ').toUpperCase() || '—'}</Badge>;
                }, width: 70 },
                { id: 'lang', header: 'Language', cell: (a: any) => getAppInfo(a.app_id)?.language || '—', width: 80 },
                { id: 'arch', header: 'Architecture', cell: (a: any) => getAppInfo(a.app_id)?.architecture || '—', width: 100 },
                { id: 'machines', header: 'Machines', cell: (a: any) => a.machine_count || getAppInfo(a.app_id)?.machine_count || 0, width: 70 },
              ]}
              items={wave.apps || []}
              variant="embedded"
              trackBy="app_id"
            />
          </Container>
        );
      })}

      {/* Conflicts */}
      {conflicts.length > 0 && (
        <Container header={<Header variant="h3">⚠️ Conflicts ({conflicts.length})</Header>}>
          <SpaceBetween size="xs">
            {conflicts.map((c: any, i: number) => (
              <Alert key={i} type="warning">{c.description || c.message || JSON.stringify(c)}</Alert>
            ))}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
};

// ── Full Wave Plan Modal (editable) ───────────────────────────────────

const FullWavePlanModal: React.FC<{ visible: boolean; onDismiss: () => void }> = ({ visible, onDismiss }) => {
  const [wavePlan, setWavePlan] = useState<any>(null);
  const [apps, setApps] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!visible) return;
    (async () => {
      setLoading(true);
      try {
        const [wpResp, appsResp] = await Promise.all([
          apiClient.get('/waves/plan'),
          apiClient.get('/portfolio/apps'),
        ]);
        setWavePlan((wpResp as any)?.data || wpResp);
        const ad = (appsResp as any)?.data || appsResp;
        setApps(Array.isArray(ad) ? ad : []);
      } catch { setWavePlan(null); }
      setLoading(false);
    })();
  }, [visible]);

  const handleMoveApp = async (appId: string, fromWave: number, newWave: number) => {
    setSaving(true);
    try {
      await apiClient.put(`/waves/assign?app_id=${appId}&wave_number=${newWave}`);
      // Optimistic local update — move app between waves
      if (wavePlan?.waves) {
        const updated = { ...wavePlan, waves: wavePlan.waves.map((w: any) => ({ ...w, apps: [...(w.apps || [])] })) };
        // Remove from old wave
        const oldWave = updated.waves.find((w: any) => w.wave_number === fromWave);
        if (oldWave) {
          const appEntry = oldWave.apps.find((a: any) => a.app_id === appId);
          oldWave.apps = oldWave.apps.filter((a: any) => a.app_id !== appId);
          // Add to new wave
          const targetWave = updated.waves.find((w: any) => w.wave_number === newWave);
          if (targetWave && appEntry) {
            targetWave.apps.push(appEntry);
          }
        }
        setWavePlan(updated);
      }
    } catch {}
    setSaving(false);
  };

  const getAppInfo = (id: string) => apps.find((a: any) => a.app_id === id) || {};
  const waves = wavePlan?.waves || [];

  return (
    <Modal visible={visible} onDismiss={onDismiss} header="Wave Plan" size="max">
      {loading ? <StatusIndicator type="loading">Loading wave plan...</StatusIndicator> : !wavePlan?.waves ? (
        <Box textAlign="center" padding="l" color="text-body-secondary">No wave plan generated yet. Use the "Generate Wave Plan" button to create one.</Box>
      ) : (
        <SpaceBetween size="m">
          {/* Summary bar */}
          <ColumnLayout columns={4} variant="text-grid">
            <Container><Box variant="awsui-key-label">Total Waves</Box><Box variant="awsui-value-large">{waves.length}</Box></Container>
            <Container><Box variant="awsui-key-label">Total Apps</Box><Box variant="awsui-value-large">{waves.reduce((s: number, w: any) => s + (w.apps?.length || 0), 0)}</Box></Container>
            <Container><Box variant="awsui-key-label">Conflicts</Box><Box variant="awsui-value-large">{(wavePlan.conflicts || []).length}</Box></Container>
            <Container><Box variant="awsui-key-label">Generated</Box><Box fontSize="body-s">{wavePlan.generated_at ? new Date(wavePlan.generated_at).toLocaleString() : '—'}</Box></Container>
          </ColumnLayout>

          {/* Waves */}
          {waves.map((wave: any) => (
            <Container key={wave.wave_number} header={<Header variant="h3" counter={`(${(wave.apps || []).length} apps)`}>{wave.label || `Wave ${wave.wave_number}`}</Header>}>
              <Table
                columnDefinitions={[
                  { id: 'app', header: 'Application', cell: (a: any) => getAppInfo(a.app_id)?.app_name || a.app_name || a.app_id, width: 180 },
                  { id: 'id', header: 'App ID', cell: (a: any) => a.app_id, width: 120 },
                  { id: 'bu', header: 'BU', cell: (a: any) => <Badge>{getAppInfo(a.app_id)?.business_unit || '—'}</Badge>, width: 80 },
                  { id: 'strategy', header: 'Strategy', cell: (a: any) => <Badge color="blue">{a.strategy || getAppInfo(a.app_id)?.migration_strategy || '—'}</Badge>, width: 100 },
                  { id: 'tier', header: 'Tier', cell: (a: any) => {
                    const tier = a.tier || getAppInfo(a.app_id)?.tier || '';
                    return <Badge color={tier === 'tier_1' ? 'red' : tier === 'tier_2' ? 'blue' : 'grey'}>{tier.replace('_', ' ').toUpperCase() || '—'}</Badge>;
                  }, width: 70 },
                  { id: 'lang', header: 'Language', cell: (a: any) => getAppInfo(a.app_id)?.language || '—', width: 80 },
                  { id: 'move', header: 'Move to Wave', cell: (a: any) => (
                    <Select
                      expandToViewport
                      selectedOption={{ value: String(wave.wave_number), label: `Wave ${wave.wave_number}` }}
                      onChange={({ detail }) => {
                        const target = parseInt(detail.selectedOption.value || '0');
                        if (target !== wave.wave_number) handleMoveApp(a.app_id, wave.wave_number, target);
                      }}
                      options={waves.map((w: any) => ({ value: String(w.wave_number), label: w.label || `Wave ${w.wave_number}` }))}
                    />
                  ), width: 130 },
                ]}
                items={wave.apps || []}
                variant="embedded"
                trackBy="app_id"
                loading={saving}
              />
            </Container>
          ))}

          {/* Complete Wave Plan button */}
          <Box float="right">
            <Button variant="primary" loading={saving} onClick={async () => {
              setSaving(true);
              try {
                // Build wave assignments from the plan
                const assignments: Record<string, number> = {};
                for (const wave of waves) {
                  for (const app of (wave.apps || [])) {
                    assignments[app.app_id] = wave.wave_number;
                  }
                }
                const allAppIds = Object.keys(assignments);
                await apiClient.post('/workflow/wave-plan/complete', {
                  app_ids: allAppIds,
                  wave_assignments: assignments,
                });
                onDismiss();
              } catch {}
              setSaving(false);
            }}>
              ✅ Complete Wave Plan
            </Button>
          </Box>
        </SpaceBetween>
      )}
    </Modal>
  );
};

// ── Portfolio Assessment Report Modal ─────────────────────────────────

const PortfolioReportModal: React.FC<{ visible: boolean; onDismiss: () => void }> = ({ visible, onDismiss }) => {
  const [report, setReport] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [meta, setMeta] = useState<any>(null);

  useEffect(() => {
    if (!visible) return;
    setLoading(true);
    apiClient.get('/workflow/wave-plan/portfolio-report').then((resp: any) => {
      const data = resp?.data || resp;
      setReport(data?.content || '');
      setMeta({ lastModified: data?.last_modified, size: data?.size, s3Key: data?.s3_key });
    }).catch(() => {
      setReport('');
      setMeta(null);
    }).finally(() => setLoading(false));
  }, [visible]);

  return (
    <Modal visible={visible} onDismiss={onDismiss} header="Portfolio Assessment Report" size="max">
      {loading ? <StatusIndicator type="loading">Loading portfolio assessment report...</StatusIndicator> : !report ? (
        <Box textAlign="center" padding="l" color="text-body-secondary">
          No portfolio assessment report found. Click "Generate Wave Plan" to run portfolio assessment first.
        </Box>
      ) : (
        <SpaceBetween size="m">
          {meta && (
            <Box fontSize="body-s" color="text-body-secondary">
              Last modified: {meta.lastModified ? new Date(meta.lastModified).toLocaleString() : '—'} | Size: {meta.size ? `${(meta.size / 1024).toFixed(1)} KB` : '—'}
            </Box>
          )}
          <MarkdownRenderer content={report} />
        </SpaceBetween>
      )}
    </Modal>
  );
};

// ── Execution Report Component (real ECS jobs + per-step progress + S3 reports) ─

const JOB_STATUS_INDICATOR: Record<string, any> = {
  RUNNING: 'in-progress', PENDING: 'pending', PROVISIONING: 'pending',
  COMPLETED: 'success', FAILED: 'error', STOPPED: 'stopped',
};
const STEP_STATUS_INDICATOR: Record<string, any> = {
  completed: 'success', running: 'in-progress', failed: 'error', pending: 'pending',
  // Honest per-step outcomes from the workbench worker.
  no_change: 'info', partial: 'warning', skipped: 'stopped',
};
const PLAN_STATUS_INDICATOR: Record<string, any> = {
  ...STEP_STATUS_INDICATOR, completed_with_errors: 'warning',
};
const PLAN_STATUS_LABEL: Record<string, string> = {
  no_change: 'No change', partial: 'Partial — build not verified', completed_with_errors: 'Completed with errors',
};
const EXEC_REPORT_LABELS: Record<string, string> = {
  'execution-database': 'Database Migration',
  'execution-infra': 'Infrastructure (IaC)',
  'execution-cicd': 'CI/CD Pipeline',
};

// Sub-step statuses written to META.exec_*_status: the database step (Harness)
// writes done/blocked; the deterministic infra/CI-CD path (tool_execution Lambda
// + infra worker) writes staged → committed|no_change → validated|plan_failed|invalid, or error.
const SUB_STATUS: Record<string, { type: any; label: string }> = {
  done: { type: 'success', label: 'Done' },
  not_required: { type: 'success', label: 'Not required' },
  committed: { type: 'success', label: 'Committed' },
  no_change: { type: 'success', label: 'Committed (no changes)' },
  validated: { type: 'success', label: 'Committed & validated' },
  plan_failed: { type: 'warning', label: 'Committed — terraform plan failed' },
  staged: { type: 'warning', label: 'Staged — not committed' },
  invalid: { type: 'error', label: 'Terraform validate failed' },
  blocked: { type: 'error', label: 'Blocked' },
  error: { type: 'error', label: 'Error' },
};
const subStatusType = (v: string): any => SUB_STATUS[v]?.type || (v ? 'in-progress' : 'pending');
const subStatusLabel = (v: string) => SUB_STATUS[v]?.label || (v ? 'Running' : 'Pending');
const titleCase = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** CloudWatch Logs console deep-link (durable — unlike an ECS task, which is
 *  pruned from the console ~1h after it stops). Falls back to the log group
 *  when the per-task stream isn't known. */
const cwLogsUrl = (group: string, stream: string, region: string): string | null => {
  if (!group) return null;
  const enc = (s: string) => encodeURIComponent(s).replace(/%/g, '$25'); // console double-encodes '/' → $252F
  const base = consoleUrl(region, `/cloudwatch/home?region=${region}#logsV2:log-groups/log-group/${enc(group)}`);
  if (!base) return null;
  return stream ? `${base}/log-events/${enc(stream)}` : base;
};

const ExecutionReportContent: React.FC<{ appId: string; app: any; wf?: any; region: string }> = ({
  appId, app, region,
}) => {
  const [exec, setExec] = useState<workflowApi.ExecutionDetails | null>(null);
  const [loading, setLoading] = useState(true);
  const [report, setReport] = useState<{ type: string; item: any } | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [artifactsOpen, setArtifactsOpen] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    workflowApi.getExecutionDetails(appId)
      .then((d) => { if (active) setExec(d); })
      .catch(() => { if (active) setExec(null); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [appId]);

  const openReport = async (type: string) => {
    // Infra/CI-CD reports are built server-side from EXECUTION# records.
    const inline = exec?.reports?.find((r) => r.type === type)?.content;
    if (inline) { setReport({ type, item: { data: { report_md: inline } } }); return; }
    setReportLoading(true);
    setReport({ type, item: null });
    try {
      const items = await getAnalysisByType(appId, type);
      setReport({ type, item: Array.isArray(items) ? items[0] : items });
    } catch { setReport({ type, item: null }); }
    setReportLoading(false);
  };

  if (loading) return <StatusIndicator type="loading">Loading execution data…</StatusIndicator>;
  if (!exec) return <Alert type="warning">Could not load execution details.</Alert>;

  const steps = exec.steps || [];
  const reports = exec.reports || [];
  const tx = exec.transform;
  const txLogUrl = tx ? cwLogsUrl(tx.log_group, tx.log_stream, region) : null;
  const artifacts = tx?.artifacts || [];
  const commits = exec.commits || [];

  return (
    <SpaceBetween size="l">
      {(exec.warnings || []).length > 0 && (
        <Alert type="warning" header={`Execution findings (${exec.warnings!.length})`}>
          <ul style={{ margin: 0, paddingLeft: 18 }}>{exec.warnings!.map((w, i) => <li key={i}>{w}</li>)}</ul>
        </Alert>
      )}
      {/* Sub-phase progress */}
      <Container header={<Header variant="h3" description="Execution runs Database → Infrastructure → CI/CD, then the approval gate.">Execution Progress</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Database Migration</Box>{app?.target_database === 'none' ? <StatusIndicator type="success">Skipped — no migration</StatusIndicator> : <StatusIndicator type={subStatusType(exec.sub_status.database)}>{subStatusLabel(exec.sub_status.database)}</StatusIndicator>}</div>
          <div><Box variant="awsui-key-label">Infrastructure</Box><StatusIndicator type={subStatusType(exec.sub_status.infra)}>{subStatusLabel(exec.sub_status.infra)}</StatusIndicator></div>
          <div><Box variant="awsui-key-label">CI/CD</Box><StatusIndicator type={subStatusType(exec.sub_status.cicd)}>{subStatusLabel(exec.sub_status.cicd)}</StatusIndicator></div>
        </ColumnLayout>
      </Container>

      {/* Code Transformation — the transform runs as ONE ECS task executing every
          step sequentially, so the task + logs live in the header and each step
          is a row (ECS-task and step tables are combined into one view). */}
      <Container
        header={
          <Header
            variant="h3"
            counter={`(${steps.length})`}
            description="One ECS Fargate task runs every step in order (ATX CLI → git commit → git push). Task id shown as its CloudWatch log stream — ECS tasks are pruned from the console after they stop."
            actions={
              <SpaceBetween size="xs" direction="horizontal">
                {artifacts.length > 0 && (
                  <Button onClick={() => setArtifactsOpen(true)}>Artifacts ({artifacts.length})</Button>
                )}
                {txLogUrl && <Button href={txLogUrl} target="_blank" iconName="external">View logs</Button>}
              </SpaceBetween>
            }
          >
            Code Transformation
          </Header>
        }
      >
        <SpaceBetween size="m">
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">ECS Task (CloudWatch)</Box>
              {tx?.task_id
                ? (txLogUrl
                    ? <Link external href={txLogUrl}>{tx.task_id.slice(0, 20)}…</Link>
                    : <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{tx.task_id}</span>)
                : <Box color="text-body-secondary">—</Box>}
            </div>
            <div><Box variant="awsui-key-label">Plan Status</Box><StatusIndicator type={PLAN_STATUS_INDICATOR[tx?.plan_status || ''] || JOB_STATUS_INDICATOR[(tx?.status || '').toUpperCase()] || 'pending'}>{PLAN_STATUS_LABEL[tx?.plan_status || ''] || tx?.plan_status || tx?.status || 'Unknown'}</StatusIndicator></div>
            <div><Box variant="awsui-key-label">Started</Box><Box fontSize="body-s">{tx?.created_at ? new Date(tx.created_at).toLocaleString() : '—'}</Box></div>
          </ColumnLayout>

          <Table
            variant="embedded"
            trackBy="step_number"
            items={steps}
            empty={<Box color="text-body-secondary">No transformation steps recorded for this run.</Box>}
            columnDefinitions={[
              { id: 'n', header: '#', width: 60, cell: (s: workflowApi.ExecutionStep) => s.step_number },
              { id: 'td', header: 'Transformation', cell: (s: workflowApi.ExecutionStep) => <code style={{ fontSize: 12 }}>{s.td_name || '—'}</code> },
              { id: 'repo', header: 'Repository', cell: (s: workflowApi.ExecutionStep) => s.repo_name ? <Box fontSize="body-s">{s.repo_name}{s.repo_branch ? ` @ ${s.repo_branch}` : ''}</Box> : <Box color="text-body-secondary">—</Box> },
              { id: 'status', header: 'Status', cell: (s: workflowApi.ExecutionStep) => <StatusIndicator type={STEP_STATUS_INDICATOR[s.status] || 'pending'}>{PLAN_STATUS_LABEL[s.status] || titleCase(s.status) || 'Pending'}</StatusIndicator> },
              {
                id: 'report', header: 'S3 Report',
                cell: (s: workflowApi.ExecutionStep) =>
                  s.s3_report
                    ? <Link external href={s.s3_report}>Report</Link>
                    : (artifacts.length > 0
                        ? <Button variant="inline-link" onClick={() => setArtifactsOpen(true)}>View</Button>
                        : <Box color="text-body-secondary">—</Box>),
              },
              { id: 'msg', header: 'Detail', cell: (s: workflowApi.ExecutionStep) => s.message ? <Box fontSize="body-s" color="text-body-secondary">{s.message}</Box> : <Box color="text-body-secondary">—</Box> },
            ]}
          />
        </SpaceBetween>
      </Container>

      {/* Repository commits — links to what execution pushed to the app repo. */}
      {commits.length > 0 && (
        <Container header={<Header variant="h3" description="Review what the execution pushed to the application's CodeCommit repository — open each commit/branch to inspect the changes.">Repository Commits</Header>}>
          <Table
            variant="embedded"
            trackBy="kind"
            items={commits}
            columnDefinitions={[
              { id: 'kind', header: 'Artifact', cell: (c: workflowApi.ExecutionCommit) => <Badge color="blue">{c.kind}</Badge>, width: 140 },
              { id: 'repo', header: 'Repository', cell: (c: workflowApi.ExecutionCommit) => <Box fontSize="body-s">{c.repo}{c.branch ? ` @ ${c.branch}` : ''}</Box> },
              { id: 'commit', header: 'Commit', cell: (c: workflowApi.ExecutionCommit) => c.commit_id ? <code style={{ fontSize: 12 }}>{c.commit_id.slice(0, 10)}</code> : <Box color="text-body-secondary">branch</Box> },
              { id: 'link', header: 'Review', cell: (c: workflowApi.ExecutionCommit) => <Link external href={c.url}>{c.commit_id ? 'View commit' : 'Browse branch'}</Link> },
            ]}
          />
        </Container>
      )}

      {/* Execution reports (S3) */}
      <Container header={<Header variant="h3" description="Reports generated during execution and stored in S3 — open for details.">Execution Reports</Header>}>
        <ColumnLayout columns={3}>
          {reports.map((r) => (
            <div key={r.type} style={{ border: '1px solid #e9ebed', borderRadius: 8, padding: 14 }}>
              <SpaceBetween size="xs">
                <Box variant="awsui-key-label">{EXEC_REPORT_LABELS[r.type] || r.type}</Box>
                <StatusIndicator type={r.present ? (['blocked', 'invalid', 'error', 'plan_failed'].includes(r.status) ? 'warning' : 'success') : 'pending'}>
                  {r.present ? (SUB_STATUS[r.status] && r.status !== 'done' ? SUB_STATUS[r.status].label : r.status === 'blocked' ? 'Blocked' : 'Generated') : 'Not generated'}
                </StatusIndicator>
                {r.timestamp && <Box fontSize="body-s" color="text-body-secondary">{new Date(r.timestamp).toLocaleString()}</Box>}
                {r.present && (
                  <SpaceBetween size="xs" direction="horizontal">
                    <Button variant="inline-link" onClick={() => openReport(r.type)}>View report</Button>
                    {!r.content && <Link external href={getAnalysisHtmlUrl(appId, r.type)}>Open full</Link>}
                  </SpaceBetween>
                )}
              </SpaceBetween>
            </div>
          ))}
        </ColumnLayout>
      </Container>

      {report && (
        <Modal
          visible
          onDismiss={() => setReport(null)}
          size="max"
          header={EXEC_REPORT_LABELS[report.type] || report.type}
          footer={<Box float="right"><Button onClick={() => setReport(null)}>Close</Button></Box>}
        >
          {reportLoading
            ? <StatusIndicator type="loading">Loading report…</StatusIndicator>
            : report.item
              ? <ReportMarkdown data={report.item} />
              : <Alert type="info">No report content available.</Alert>}
        </Modal>
      )}

      {artifactsOpen && (
        <Modal
          visible
          onDismiss={() => setArtifactsOpen(false)}
          size="large"
          header="Transformation artifacts (S3)"
          footer={<Box float="right"><Button onClick={() => setArtifactsOpen(false)}>Close</Button></Box>}
        >
          <Table
            variant="embedded"
            trackBy="key"
            items={artifacts}
            empty={<Box color="text-body-secondary">No artifacts found.</Box>}
            columnDefinitions={[
              { id: 'run', header: 'ATX run', cell: (a: workflowApi.ExecutionArtifact) => <Box fontSize="body-s">{a.run_id || '—'}</Box> },
              { id: 'name', header: 'Artifact', cell: (a: workflowApi.ExecutionArtifact) => <Link external href={a.url}>{a.name}</Link> },
              { id: 'size', header: 'Size', cell: (a: workflowApi.ExecutionArtifact) => <Box fontSize="body-s">{a.size ? `${(a.size / 1024).toFixed(1)} KB` : '—'}</Box> },
            ]}
          />
        </Modal>
      )}
    </SpaceBetween>
  );
};

// ── Task Log Viewer ────────────────────────────────────────────────────

// ── Handover Content Component ────────────────────────────────────────

const HandoverContent: React.FC<{ appId: string; strategy: string; app: any }> = ({ appId, strategy, app }) => {
  const [handoverData, setHandoverData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    apiClient.get(`/workflow/handover/${appId}`).then((resp: any) => {
      const data = (resp as any)?.data ?? resp;
      setHandoverData(data);
    }).catch(() => setHandoverData(null)).finally(() => setLoading(false));
  }, [appId]);

  if (loading) return <StatusIndicator type="loading">Loading handover data...</StatusIndicator>;

  // Retire path — decommission plan
  if (strategy === 'retire') {
    return (
      <Container header={<Header variant="h3">Decommission Plan</Header>}>
        <SpaceBetween size="m">
          <Alert type="info">Application marked for retirement. Review the decommission plan and approve to complete.</Alert>
          <ColumnLayout columns={2} variant="text-grid">
            <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
            <div><Box variant="awsui-key-label">Strategy</Box><Badge color="grey">Retire</Badge></div>
            <div><Box variant="awsui-key-label">Current Compute</Box><Box>{app?.current_compute || 'EC2'} ({app?.machine_count || 0} servers)</Box></div>
            <div><Box variant="awsui-key-label">Database</Box><Box>{app?.database?.engine || '—'} ({app?.database?.size_gb || 0} GB)</Box></div>
          </ColumnLayout>
          {handoverData?.decommission_plan ? (
            <Container header={<Header variant="h3">Generated Decommission Plan</Header>}>
              <div className="markdown-body" style={{fontSize: 13}}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{handoverData.decommission_plan}</ReactMarkdown>
              </div>
            </Container>
          ) : (
            <Container header={<Header variant="h3">Shutdown Schedule</Header>}>
              <div style={{fontSize: 13}}>
                <strong>Week 1-2:</strong> Notify stakeholders, redirect traffic, disable new connections<br/>
                <strong>Week 3:</strong> Stop application services, take final backups<br/>
                <strong>Week 4:</strong> Archive data to S3 Glacier, terminate compute instances<br/>
                <strong>Week 5:</strong> Remove DNS records, release IP addresses, delete security groups<br/>
                <strong>Week 6:</strong> Final cost verification, close change tickets
              </div>
            </Container>
          )}
        </SpaceBetween>
      </Container>
    );
  }

  // Non-retire path — full handover package
  return (
    <Container header={<Header variant="h3">Handover Package</Header>}>
      <SpaceBetween size="m">
        <ColumnLayout columns={2} variant="text-grid">
          <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
          <div><Box variant="awsui-key-label">Target Compute</Box><Badge color={app?.target_compute ? 'blue' : 'grey'}>{app?.target_compute || 'Not recorded'}</Badge></div>
          <div><Box variant="awsui-key-label">Handover To</Box><Box>Platform Engineering Team</Box></div>
          <div><Box variant="awsui-key-label">Status</Box><StatusIndicator type={handoverData?.status === 'completed' ? 'success' : 'info'}>{handoverData?.status || 'Pending'}</StatusIndicator></div>
        </ColumnLayout>
        {handoverData?.runbook ? (
          <Container header={<Header variant="h3">Operational Runbook</Header>}>
            <div className="markdown-body" style={{fontSize: 13, maxHeight: 500, overflow: 'auto'}}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{handoverData.runbook}</ReactMarkdown>
            </div>
          </Container>
        ) : (
          <Container header={<Header variant="h3">Expected Documents (from handover skill)</Header>}>
            <div style={{fontSize: 13}}>
              ⏳ <strong>Operational Runbook</strong> — restart, scaling, log access, alarm response, rollback<br/>
              ⏳ <strong>Architecture Reference</strong> — VPC, subnets, security groups, resource ARNs<br/>
              ⏳ <strong>DR Playbook</strong> — failover procedure, RTO/RPO validation, backup schedule<br/>
              ⏳ <strong>Monitoring Guide</strong> — CloudWatch dashboards, alarms, on-call setup<br/>
              ⏳ <strong>Decommission Plan</strong> — old infrastructure shutdown timeline (2-week bake)<br/>
              ⏳ <strong>Knowledge Transfer</strong> — migration decisions, trade-offs, lessons learned
            </div>
            <Box color="text-body-secondary" fontSize="body-s">Documents will be generated when the handover agent runs.</Box>
          </Container>
        )}
        {handoverData?.decommission_plan && (
          <Container header={<Header variant="h3">Decommission Plan (Old Infrastructure)</Header>}>
            <div className="markdown-body" style={{fontSize: 13, maxHeight: 300, overflow: 'auto'}}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{handoverData.decommission_plan}</ReactMarkdown>
            </div>
          </Container>
        )}
      </SpaceBetween>
    </Container>
  );
};

// ── AgentCore Execution Details (standalone — always rendered last) ────

// What each MCP tool does — surfaced in the tool-call popup so the user knows
// the intent behind the call, not just its raw payload.
const TOOL_PURPOSE: Record<string, string> = {
  get_app_context: 'Loads the full application profile (metadata, tech stack, strategy) the agent reasons over.',
  calculate_completeness: 'Scores how complete the discovery data is and flags missing fields.',
  recommend_strategy: 'Recommends a 7R migration strategy with a confidence score and rationale.',
  update_app_field: 'Writes a field back to the application record (strategy, status, etc.).',
  get_analysis_result: 'Retrieves stored code-analysis / assessment findings for the app.',
  store_analysis_result: 'Persists assessment / analysis findings for the app.',
  start_code_analysis: 'Kicks off the AWS Transform (ATX) code-analysis run on ECS.',
  start_code_transformation: 'Kicks off the AWS Transform (ATX) code-transformation run on ECS.',
  get_design_document: 'Retrieves the current target-architecture design document.',
  store_design_document: 'Persists the target-architecture design document (versioned).',
  store_migration_plan: 'Persists the wave / migration execution plan.',
  provision_infrastructure: 'Renders and stages the Terraform IaC for the target architecture.',
  generate_cicd: 'Renders and stages the CI/CD pipeline definition for the target.',
  commit_cicd_to_repo: 'Commits the generated pipeline files to the linked migration branch.',
  compute_risk_score: 'Computes the deterministic migration risk score (complexity, dependencies, data, criticality).',
  run_terraform_plan: 'Runs terraform init / validate / plan on the staged IaC in an ECS worker (nothing is applied).',
};

// Who calls a tool: the Harness agent (chooses tools from its phase prompt) or a
// deterministic Step Functions state (ASL invokes the tool Lambda directly).
type ToolCaller = 'agent' | 'workflow';

interface PhaseToolPlanItem {
  tool: string;
  target: string;
  by: ToolCaller;
  /** ASL state that invokes it (workflow tools only). */
  state?: string;
  /** Only runs for strategies that transform code (replatform/refactor). */
  transformOnly?: boolean;
}

// Mirrors infra/step-functions/migration-workflow.v2.asl.json — agent tools are
// the ones each invokeHarness prompt tells the Harness to call; workflow tools
// are the lambda:invoke states. Keep in sync with the ASL.
const PHASE_TOOL_PLAN: Record<string, PhaseToolPlanItem[]> = {
  discovery: [
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'calculate_completeness', target: 'Strategy', by: 'agent' },
    { tool: 'recommend_strategy', target: 'Strategy', by: 'agent' },
    { tool: 'update_app_field', target: 'Portfolio', by: 'agent' },
    { tool: 'compute_risk_score', target: 'Documents', by: 'agent', state: 'Discovery (fallback: Score_Discovery)' },
  ],
  assessment: [
    { tool: 'start_code_analysis', target: 'Execution', by: 'workflow', state: 'LaunchATX' },
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'get_analysis_result', target: 'Documents', by: 'agent' },
    { tool: 'store_analysis_result', target: 'Documents', by: 'agent' },
    { tool: 'update_app_field', target: 'Portfolio', by: 'agent' },
  ],
  design: [
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'get_analysis_result', target: 'Documents', by: 'agent' },
    { tool: 'store_design_document', target: 'Documents', by: 'agent' },
  ],
  execution: [
    { tool: 'start_code_transformation', target: 'Execution', by: 'workflow', state: 'LaunchTransformation', transformOnly: true },
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'store_analysis_result', target: 'Documents', by: 'agent' },
    { tool: 'update_app_field', target: 'Portfolio', by: 'agent' },
    { tool: 'provision_infrastructure', target: 'Execution', by: 'workflow', state: 'InfrastructureProvisioning' },
    { tool: 'run_terraform_plan', target: 'Execution', by: 'workflow', state: 'InfrastructureValidate' },
    { tool: 'generate_cicd', target: 'Execution', by: 'workflow', state: 'CICDSetup' },
  ],
  validation: [
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'store_analysis_result', target: 'Documents', by: 'agent' },
    { tool: 'update_app_field', target: 'Portfolio', by: 'agent' },
  ],
  cutover: [
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'store_analysis_result', target: 'Documents', by: 'agent' },
    { tool: 'update_app_field', target: 'Portfolio', by: 'agent' },
  ],
  handover: [
    { tool: 'get_app_context', target: 'Portfolio', by: 'agent' },
    { tool: 'store_design_document', target: 'Documents', by: 'agent' },
    { tool: 'update_app_field', target: 'Portfolio', by: 'agent' },
  ],
};

// Tool → caller for this phase; tools outside the plan default to the agent.
const callerOf = (ph: string, tool: string): ToolCaller =>
  (PHASE_TOOL_PLAN[ph] || []).find((t) => t.tool === tool)?.by || 'agent';
// Who actually made this call — recorded on the trace; the static plan is only a fallback.
const callerOfCall = (ph: string, c: workflowApi.ToolCall): ToolCaller =>
  c.caller === 'agent' || c.caller === 'workflow' ? c.caller : callerOf(ph, c.tool);
const CallerBadge: React.FC<{ by: ToolCaller }> = ({ by }) =>
  by === 'workflow' ? <Badge color="grey">Workflow</Badge> : <Badge color="blue">Agent</Badge>;

// Truncate a scalar for compact key–value display.
const _short = (v: any, max = 140): string => {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s && s.length > max ? s.slice(0, max) + '…' : s;
};

// Derive a one-line, human-readable insight from a tool's parsed output/input.
// Resilient to key drift — falls back to a generic status line.
const summarizeCall = (c: workflowApi.ToolCall): string => {
  const o = (c.output && typeof c.output === 'object') ? c.output as any : {};
  const i = (c.input && typeof c.input === 'object') ? c.input as any : {};
  if (c.status === 'error') return String(o.error || 'Tool failed');
  switch (c.tool) {
    case 'recommend_strategy': {
      const s = o.strategy || o.recommended_strategy;
      const conf = o.confidence ?? o.confidence_score;
      return s ? `Recommended ${s}${conf != null ? ` · ${Math.round(Number(conf) * (Number(conf) <= 1 ? 100 : 1))}%` : ''}` : 'Strategy recommendation';
    }
    case 'calculate_completeness': {
      const sc = o.score ?? o.completeness_score ?? o.completeness;
      const miss = Array.isArray(o.missing_fields) ? o.missing_fields.length : undefined;
      return sc != null ? `Completeness ${Math.round(Number(sc) * (Number(sc) <= 1 ? 100 : 1))}%${miss ? ` · ${miss} missing` : ''}` : 'Completeness scored';
    }
    case 'update_app_field':
      return i.field ? `Set ${i.field} = ${_short(i.value, 60)}` : 'Updated app field';
    case 'get_app_context':
      return o.app_name ? `Loaded ${o.app_name}` : 'Loaded app context';
    case 'store_design_document':
      return `Stored design document${o.version ? ` (v${o.version})` : ''}`;
    case 'get_design_document':
      return o.version ? `Design document v${o.version}` : 'Retrieved design document';
    case 'store_migration_plan': {
      const waves = o.waves ?? (Array.isArray(o.plan) ? o.plan.length : undefined);
      return `Stored migration plan${waves != null ? ` · ${waves} waves` : ''}`;
    }
    case 'store_analysis_result':
      return `Stored analysis${o.type || i.type ? `: ${o.type || i.type}` : ''}`;
    case 'get_analysis_result':
      return o.type ? `Analysis: ${o.type}` : 'Retrieved analysis';
    case 'start_code_analysis':
      return `Started code analysis${o.job_id || o.task_arn ? ` · ${_short(o.job_id || o.task_arn, 40)}` : ' on ECS'}`;
    case 'start_code_transformation':
      return `Started code transformation${o.job_id || o.task_arn ? ` · ${_short(o.job_id || o.task_arn, 40)}` : ' on ECS'}`;
    case 'provision_infrastructure':
      return `Staged IaC${o.files ? ` · ${Array.isArray(o.files) ? o.files.length : o.files} files` : ''}`;
    case 'generate_cicd':
      return `Staged CI/CD pipeline${o.files ? ` · ${Array.isArray(o.files) ? o.files.length : o.files} files` : ''}`;
    default:
      return o.message || o.summary || (o.status ? `Status: ${o.status}` : 'Completed');
  }
};

// Render a parsed object as a readable label → value list (one row per field,
// label and value on the same line). Falls back to a preformatted block for
// non-objects (strings, arrays, primitives).
const KeyValueView: React.FC<{ value: any }> = ({ value }) => {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) return <Box fontSize="body-s" color="text-body-secondary">(empty)</Box>;
    return (
      <div>
        {entries.map(([k, v]) => {
          const nested = v !== null && typeof v === 'object';
          return (
            <div
              key={k}
              style={{ display: 'flex', gap: 12, padding: '4px 0', borderBottom: '1px solid var(--color-border-divider-secondary, #e9ebed)', alignItems: 'baseline' }}
            >
              <div style={{ flex: '0 0 160px', minWidth: 120 }}>
                <Box variant="awsui-key-label" fontSize="body-s">{k}</Box>
              </div>
              <div style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>
                {nested
                  ? <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 12 }}>{_short(JSON.stringify(v, null, 2), 800)}</pre>
                  : <Box fontSize="body-s">{String(v)}</Box>}
              </div>
            </div>
          );
        })}
      </div>
    );
  }
  return <Box variant="code"><pre style={{ whiteSpace: 'pre-wrap', margin: 0, maxHeight: 320, overflow: 'auto' }}>{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre></Box>;
};

const ExecutionDetails: React.FC<{ appId: string; phase?: string; pipelineApps: workflowApi.ActiveWorkflow[]; strategy?: string }> = ({ appId, phase, pipelineApps, strategy }) => {
  const wf = pipelineApps.find(w => w.app_id === appId);
  const { aws_region: region, agentcore_harness_id: harnessId, bedrock_model_id: modelId } = useCapabilities();
  const executionName = wf?.execution_arn?.split(':').pop() || '';
  const sfnName = wf?.execution_arn?.split(':')[6]?.split('/').pop() || '';
  const cwLogsUrl = consoleUrl(region, `/states/home?region=${region}#/v2/executions/details/${wf?.execution_arn}`);
  const harnessUrl = consoleUrl(region, `/bedrock/home?region=${region}#/agentcore/harnesses/${harnessId}`);
  const ph = phase || wf?.current_phase || '';

  // Captured MCP tool-call traces for this phase — each clickable to inspect input/output in-app.
  const [calls, setCalls] = useState<workflowApi.ToolCall[]>([]);
  const [selected, setSelected] = useState<workflowApi.ToolCall | null>(null);
  useEffect(() => {
    if (!appId) return;
    let active = true;
    // Filter to this phase when known; otherwise show all captured calls for the app.
    workflowApi.getToolCalls(appId, ph)
      .then((data) => { if (active) setCalls(Array.isArray(data) ? data : []); })
      .catch(() => { if (active) setCalls([]); });
    return () => { active = false; };
  }, [appId, ph, wf?.current_step]);

  const plannedTools = PHASE_TOOL_PLAN[ph] || [];
  const agentPlanned = plannedTools.filter((t) => t.by === 'agent').length;
  const workflowPlanned = plannedTools.length - agentPlanned;
  const agentCalls = calls.filter((c) => callerOfCall(ph, c) === 'agent').length;
  const distinctCalled = new Set(calls.map((c) => c.tool)).size;

  return (
    <Container header={<Header variant="h3">AgentCore Execution Details</Header>}>
      <SpaceBetween size="s">
        <ColumnLayout columns={2} variant="text-grid">
          <div><Box variant="awsui-key-label">Step Functions Execution</Box><Box fontSize="body-s">{cwLogsUrl ? <a href={cwLogsUrl} target="_blank" rel="noopener">{executionName || '—'}</a> : (executionName || '—')}</Box></div>
          <div><Box variant="awsui-key-label">State Machine</Box><Box fontSize="body-s">{sfnName || '—'}</Box></div>
          <div><Box variant="awsui-key-label">Started</Box><Box>{wf?.started_at ? new Date(wf?.started_at).toLocaleString() : '—'}</Box></div>
          <div><Box variant="awsui-key-label">Current Phase</Box><Badge color={wf?.status === 'RUNNING' ? 'blue' : 'grey'}>{wf?.current_phase || '—'}</Badge></div>
          <div><Box variant="awsui-key-label">Current Step</Box><Box>{wf?.current_step || '—'}</Box></div>
          <div><Box variant="awsui-key-label">Deployment Mode</Box><Badge color="green">AgentCore Harness</Badge></div>
        </ColumnLayout>
        <ColumnLayout columns={2} variant="text-grid">
          <div><Box variant="awsui-key-label">Harness</Box><Box fontSize="body-s">{harnessId ? (harnessUrl ? <a href={harnessUrl} target="_blank" rel="noopener">{harnessId}</a> : harnessId) : '—'}</Box></div>
          <div><Box variant="awsui-key-label">Model</Box><Box fontSize="body-s">{modelId || '—'}</Box></div>
        </ColumnLayout>
        {plannedTools.length > 0 && (
          <div>
            <Box variant="awsui-key-label">Tool plan ({plannedTools.length}) — {agentPlanned} agent · {workflowPlanned} workflow</Box>
            <Box fontSize="body-s" color="text-body-secondary">
              Agent tools are chosen by the AgentCore Harness from its phase prompt; workflow tools are invoked deterministically by a Step Functions state.
            </Box>
            <Table
              variant="embedded"
              items={plannedTools}
              columnDefinitions={[
                { id: 'tool', header: 'Tool', cell: (item: PhaseToolPlanItem) => <Box fontWeight="bold">{item.tool}</Box>, width: 210 },
                { id: 'by', header: 'Called by', cell: (item: PhaseToolPlanItem) => <SpaceBetween size="xxs" direction="horizontal"><CallerBadge by={item.by} />{item.state && <Box fontSize="body-s" color="text-body-secondary">{item.state}</Box>}</SpaceBetween>, width: 190 },
                { id: 'target', header: 'Target', cell: (item: PhaseToolPlanItem) => <Badge>{item.target}</Badge>, width: 120 },
                { id: 'purpose', header: 'What it does', cell: (item: PhaseToolPlanItem) => TOOL_PURPOSE[item.tool] || 'Runs a governed agent action for this phase.' },
                {
                  id: 'status',
                  header: 'Evidence',
                  width: 130,
                  cell: (item: PhaseToolPlanItem) => {
                    const matching = calls.filter(c => c.tool === item.tool);
                    const call = matching[0];
                    if (!call && item.transformOnly && strategy && !transformsAllowedFor(strategy)) {
                      return <StatusIndicator type="stopped">{`N/A — ${strategy}`}</StatusIndicator>;
                    }
                    return call ? (
                      <StatusIndicator type={call.status === 'error' ? 'error' : 'success'}>
                        {call.status === 'error' ? 'Failed' : matching.length > 1 ? `Called ×${matching.length}` : 'Called'}
                      </StatusIndicator>
                    ) : (
                      <Popover
                        dismissButton={false}
                        triggerType="text"
                        size="small"
                        content="Expected tool for this phase. No execution trace was recorded — this does not indicate the tool ran or failed. Some phases (e.g. Handover) finalize deterministically without every planned call."
                      >
                        <StatusIndicator type="info">Planned</StatusIndicator>
                      </Popover>
                    );
                  },
                },
              ]}
            />
          </div>
        )}
        <div>
          <Box variant="awsui-key-label">
            Tools Called ({calls.length}){calls.length > 0 && ` — ${agentCalls} agent · ${calls.length - agentCalls} workflow${distinctCalled < calls.length ? ` · ${distinctCalled} distinct` : ''}`}
          </Box>
          {calls.length === 0 ? (
            <Box color="text-body-secondary" fontSize="body-s">
              No tool calls recorded for this phase yet. Traces appear here as the Harness invokes tools; full spans are in the {cwLogsUrl ? <a href={cwLogsUrl} target="_blank" rel="noopener">Step Functions console</a> : 'Step Functions console'}.
            </Box>
          ) : (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
              {calls.map((c, i) => (
                <Button key={i} variant="inline-link" onClick={() => setSelected(c)}>
                  <StatusIndicator type={c.status === 'error' ? 'error' : 'success'}>
                    {c.tool}{c.latency_ms ? ` · ${c.latency_ms}ms` : ''}
                  </StatusIndicator>
                  {' '}<CallerBadge by={callerOfCall(ph, c)} />
                </Button>
              ))}
            </div>
          )}
        </div>
      </SpaceBetween>

      {selected && (
        <Modal
          visible
          onDismiss={() => setSelected(null)}
          header={`${selected.target} · ${selected.tool}`}
          size="large"
          footer={<Box float="right"><Button onClick={() => setSelected(null)}>Close</Button></Box>}
        >
          <SpaceBetween size="m">
            {TOOL_PURPOSE[selected.tool] && (
              <Box color="text-body-secondary" fontSize="body-s">{TOOL_PURPOSE[selected.tool]}</Box>
            )}
            <Alert type={selected.status === 'error' ? 'error' : 'success'}>{summarizeCall(selected)}</Alert>
            <ColumnLayout columns={4} variant="text-grid">
              <div><Box variant="awsui-key-label">Phase</Box><Badge>{selected.phase || ph || '—'}</Badge></div>
              <div><Box variant="awsui-key-label">Target Lambda</Box><Box fontSize="body-s">{selected.target || '—'}</Box> <CallerBadge by={callerOfCall(selected.phase || ph, selected)} /></div>
              <div><Box variant="awsui-key-label">Latency</Box><Box>{selected.latency_ms} ms</Box></div>
              <div><Box variant="awsui-key-label">When</Box><Box fontSize="body-s">{selected.at ? new Date(selected.at).toLocaleString() : '—'}</Box></div>
            </ColumnLayout>
            <div>
              <Box variant="awsui-key-label">Input</Box>
              <KeyValueView value={selected.input} />
            </div>
            <div>
              <Box variant="awsui-key-label">Output</Box>
              <KeyValueView value={selected.output} />
            </div>
            <ExpandableSection headerText="Raw JSON" variant="footer">
              <Box variant="code"><pre style={{ whiteSpace: 'pre-wrap', margin: 0, maxHeight: 360, overflow: 'auto' }}>{JSON.stringify({ input: selected.input, output: selected.output }, null, 2)}</pre></Box>
            </ExpandableSection>
          </SpaceBetween>
        </Modal>
      )}
    </Container>
  );
};

// ── Report Content Component ──────────────────────────────────────────

const ReportContent: React.FC<{ appId: string; phase: string; apps: any[]; pipelineApps: workflowApi.ActiveWorkflow[]; hideExecutionDetails?: boolean }> = ({ appId, phase, apps, pipelineApps, hideExecutionDetails }) => {
  const app = apps.find(a => a.app_id === appId);
  const wf = pipelineApps.find(w => w.app_id === appId);
  const caps = useCapabilities();
  const regionOpts = targetRegionOptions(caps);
  const computeOpts = computeOptionsFor(app?.migration_strategy);
  const databaseOpts = databaseOptionsFor(app?.migration_strategy);
  const [designDoc, setDesignDoc] = useState<string>('');
  const [loadingDoc, setLoadingDoc] = useState(phase === 'design');
  const [inputValues, setInputValues] = useState<Record<string, any>>({});

  const updateInputValue = (field: string, value: any) => {
    setInputValues(prev => {
      const next = { ...prev, [field]: value };
      return next;
    });
  };

  useEffect(() => {
    // Always load design doc (it's generated during multiple phases)
    setLoadingDoc(true);
    apiClient.get(`/design/${appId}`).then((resp: any) => {
      setDesignDoc(resp?.content || resp?.data?.content || '');
    }).catch(() => setDesignDoc('')).finally(() => setLoadingDoc(false));
  }, [appId, phase]);

  // Execution details. Gates that append selection UI after the report set
  // hideExecutionDetails and render <ExecutionDetails> themselves at the very end.
  const TaskDetails = () => hideExecutionDetails ? null : <ExecutionDetails appId={appId} phase={phase} pipelineApps={pipelineApps} strategy={app?.migration_strategy} />;

  // Status of THIS phase (Done / current status / Not Started), not the workflow's
  // overall status — otherwise every past phase reads "Awaiting Approval" while
  // the workflow waits at a later gate.
  const phaseStatus = phaseStatusLabel(wf as any, phase, app?.migration_strategy);
  const PhaseStatusField = () => (
    <div><Box variant="awsui-key-label">Status</Box><StatusIndicator type={phaseStatus.type}>{phaseStatus.label}</StatusIndicator></div>
  );

  // ── Discovery phase ─────────────────────────────────────────────────
  if (phase === 'discovery') {
    // Single status source (R10): Running / Awaiting Approval / Done / Failed.
    const status = statusLabel(wf as any);
    // Discovery is genuinely still running only when the phase is discovery AND
    // it is NOT yet at the review gate. Strategy is shown once discovery has
    // reached its approval gate (i.e. recommend_strategy returned) — not from a
    // stale pre-existing value while the agent is still computing.
    const discoveryComputing = status.label === 'Running' && getPhaseIndex(wf?.current_phase) === 0;
    const strategyReady = !!app?.strategy_confidence && !discoveryComputing;
    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
          <div><Box variant="awsui-key-label">Strategy</Box>{strategyReady ? <Badge color="blue">{app?.migration_strategy}</Badge> : <Box color="text-body-secondary" fontSize="body-s">{discoveryComputing ? 'Computing…' : '—'}</Box>}</div>
          <PhaseStatusField />
        </ColumnLayout>
        <Container header={<Header variant="h3">Discovery Summary</Header>}>
          <ColumnLayout columns={4} variant="text-grid">
            <div><Box variant="awsui-key-label">Language</Box><Box>{app?.language || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Framework</Box><Box>{app?.framework || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Architecture</Box><Box>{app?.architecture || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Current Compute</Box><Box>{app?.current_compute || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Database</Box><Box>{app?.database?.engine ? `${app.database.engine} ${app.database.version || ''}` : '—'}</Box></div>
            <div><Box variant="awsui-key-label">Tier</Box><Badge color={app?.tier === 'tier_1' ? 'red' : 'blue'}>{app?.tier || '—'}</Badge></div>
            <div><Box variant="awsui-key-label">Owners</Box><Box>{app?.owners?.length || 0}</Box></div>
            <div><Box variant="awsui-key-label">Repos</Box><Box>{app?.repo_count || app?.repos?.length || 0}</Box></div>
          </ColumnLayout>
        </Container>
        <RiskScorePanel appId={appId} />
        {strategyReady && (
          <Container header={<Header variant="h3">Strategy Recommendation</Header>}>
            <SpaceBetween size="m">
              <ColumnLayout columns={3} variant="text-grid">
                <div><Box variant="awsui-key-label">Recommended Strategy</Box><Box fontSize="heading-l">{app.migration_strategy?.toUpperCase()}</Box></div>
                <div><Box variant="awsui-key-label">Confidence</Box><Box fontSize="heading-l">{app?.strategy_confidence ? `${parseFloat(app.strategy_confidence) <= 1 ? Math.round(parseFloat(app.strategy_confidence) * 100) : Math.round(parseFloat(app.strategy_confidence))}%` : '—'}</Box></div>
                <div><Box variant="awsui-key-label">Rationale</Box><Box>{app?.strategy_rationale || (app?.risks?.length ? app.risks.join('; ') : '—')}</Box></div>
              </ColumnLayout>

              <Header variant="h3">Rules Applied</Header>
              <Table
                columnDefinitions={[
                  { id: 'factor', header: 'Decision Factor', cell: (r: any) => <Box fontWeight="bold">{r.factor}</Box>, width: 200 },
                  { id: 'value', header: 'Value', cell: (r: any) => r.value, width: 200 },
                  { id: 'impact', header: 'Impact on Strategy', cell: (r: any) => r.impact },
                ]}
                items={[
                  { factor: 'Tier', value: app.tier?.replace('_', ' ')?.toUpperCase() || '—', impact: app.tier === 'tier_1' ? 'High-value app → favors Refactor/Replatform' : app.tier === 'tier_3' ? 'Low-value → favors Rehost/Retire' : 'Standard priority' },
                  { factor: 'Architecture', value: app.architecture || '—', impact: app.architecture === 'monolith' ? 'Monolith → candidate for containerization (Replatform) or decomposition (Refactor)' : app.architecture === 'microservices' ? 'Already decomposed → Rehost or cloud-optimize' : 'SOA → Replatform to managed services' },
                  { factor: 'Language', value: app.language || '—', impact: ['Java', 'Python', 'Node.js', 'C#', 'Go'].includes(app.language) ? `${app.language} is cloud-friendly → Replatform/Refactor viable` : app.language ? `${app.language} — verify cloud runtime support` : 'Unknown — needs discovery' },
                  { factor: 'Database', value: app?.database?.engine ? `${app.database.engine} ${app.database.version || ''} (${app.database.licensing || 'unknown'})` : '—', impact: ['Oracle', 'SQL Server'].includes(app?.database?.engine) && app?.database?.licensing === 'enterprise' ? `Licensed DB → Replatform to Aurora/RDS saves $${(app?.current_cost?.annual_license || 0).toLocaleString()}/yr` : app?.database?.engine ? 'Open-source DB — straightforward migration' : 'No DB info — needs discovery' },
                  { factor: 'Current Compute', value: app?.current_compute || '—', impact: app?.current_compute === 'vmware' ? 'VMware → Replatform (containerize) or Relocate (VMC on AWS)' : app?.current_compute === 'bare_metal' ? 'Bare metal → Rehost to EC2 or Replatform to containers' : app?.current_compute === 'ecs' || app?.current_compute === 'eks' ? 'Already containerized → optimize or Retain' : 'Unknown compute' },
                  { factor: 'Deployment', value: app?.deployment_method || '—', impact: app?.deployment_method === 'manual' ? 'Manual deployment → Replatform adds CI/CD value' : app?.deployment_method === 'container' ? 'Already containerized → lower migration effort' : 'CI/CD exists — migration-ready' },
                  { factor: 'Active Development', value: app?.change_history?.active_development ? 'Yes' : 'No', impact: app?.change_history?.active_development ? `Team size ${app?.change_history?.team_size || '?'} — active investment justifies modernization` : 'No active dev — minimize effort (Rehost/Retire)' },
                  { factor: 'Annual License Cost', value: app?.current_cost?.annual_license ? `$${app.current_cost.annual_license.toLocaleString()}` : '—', impact: (app?.current_cost?.annual_license || 0) > 30000 ? 'High license cost → Replatform to open-source/managed saves significant spend' : 'Low/no license cost — not a migration driver' },
                  { factor: 'Constraints', value: app?.constraints?.hard_deadline || app?.constraints?.dc_lease_end || '—', impact: app?.constraints?.hard_deadline ? `Deadline ${app.constraints.hard_deadline} — tight timeline favors Rehost` : app?.constraints?.dc_lease_end ? `DC lease end ${app.constraints.dc_lease_end} — must migrate` : 'No hard constraints' },
                ].filter(r => r.value !== '—')}
                variant="embedded"
              />
            </SpaceBetween>
          </Container>
        )}
        {!strategyReady && (
          <Container header={<Header variant="h3">Strategy Recommendation</Header>}>
            <SpaceBetween size="s">
              <StatusIndicator type={discoveryComputing ? 'loading' : 'info'}>
                {discoveryComputing
                  ? 'Discovery agent is analyzing application data and computing strategy recommendation...'
                  : 'Discovery agent did not produce a strategy recommendation. Retry with additional input or approve with a manually selected strategy.'}
              </StatusIndicator>
              {app?.migration_strategy && !discoveryComputing && (
                <Box>Pre-existing strategy from enrichment data: <Badge color="blue">{app.migration_strategy}</Badge> (not agent-validated)</Box>
              )}
            </SpaceBetween>
          </Container>
        )}
        <TaskDetails />
        
      </SpaceBetween>
    );
  }

  // Design phase — show the design document + execution details
  if (phase === 'design') {

    // Parse design doc for actual gaps — skip fields that already have data
    const extractRequiredInputs = (content: string): { field: string; context: string; line: number }[] => {
      const inputs: { field: string; context: string; line: number }[] = [];
      const seenFields = new Set<string>();
      const lines = content.split('\n');
      
      // Fields already populated — don't ask for these
      const existingData = new Set<string>();
      if (app?.business_unit) existingData.add('business unit');
      if (app?.language) existingData.add('language');
      if (app?.framework) existingData.add('framework');
      if (app?.architecture) existingData.add('architecture');
      if (app?.tier) existingData.add('tier');
      if (app?.deployment_method) { existingData.add('deployment'); existingData.add('deployment method'); }
      if (app?.data_classification) { existingData.add('data classification'); existingData.add('classification'); }
      if (app?.migration_strategy) { existingData.add('strategy'); existingData.add('migration strategy'); }
      if (app?.current_compute) { existingData.add('compute'); existingData.add('current compute'); }
      if (app?.current_region) { existingData.add('region'); existingData.add('current region'); }
      if (app?.database?.engine) { existingData.add('database'); existingData.add('db engine'); existingData.add('engine'); }
      if (app?.runtime?.os) existingData.add('os');
      if (app?.runtime?.app_server) existingData.add('app server');
      if (app?.user_count) { existingData.add('user count'); existingData.add('users'); }
      if (app?.data_volume_gb) existingData.add('data volume');
      if (app?.rpo) existingData.add('rpo');
      if (app?.rto) existingData.add('rto');
      if (app?.current_cost?.monthly_infra) { existingData.add('monthly'); existingData.add('infra cost'); }
      if (app?.current_cost?.annual_license) { existingData.add('license'); existingData.add('annual'); }
      if (app?.containerization) existingData.add('containerization');
      if (app?.cicd_target) { existingData.add('ci/cd'); existingData.add('pipeline'); }
      if (app?.business_unit) existingData.add('business unit');
      if ((app?.owners||[]).length > 0) existingData.add('owner');
      if ((app?.repos||[]).length > 0) { existingData.add('repo'); existingData.add('repository'); }
      if ((app?.compliance||[]).length > 0) existingData.add('compliance');
      if (app?.multi_region && app.multi_region !== 'no') existingData.add('multi-region');
      if (app?.encryption_required) existingData.add('encryption');
      
      // Meta words that are prose/status text, not real input fields — never surface these.
      const FIELD_DENYLIST = ['status', 'version', 'blocked', 'execution', 'note', 'summary', 'phase', 'state', 'readiness'];

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const upperLine = line.toUpperCase();
        if (!upperLine.includes('REQUIRES INPUT') && !upperLine.includes('REQUIRES VALIDATION')) continue;
        // Skip blockquotes / prose — real inputs live in table rows, not narrative lines.
        if (line.trim().startsWith('>')) continue;

        const tableMatch = line.match(/\|\s*([^|]{3,40}?)\s*\|\s*\[?([^|\]]*REQUIRES[^|\]]*)\]?\s*\|/i);
        let field = '';
        let context = '';
        if (tableMatch) {
          field = tableMatch[1].trim().replace(/\*\*/g, '');
          context = tableMatch[2].trim();
        } else {
          const cleanLine = line.replace(/[*#`]/g, '').trim();
          const colonIdx = cleanLine.indexOf(':');
          if (colonIdx > 0 && colonIdx < 50) {
            field = cleanLine.substring(0, colonIdx).trim();
            context = cleanLine.substring(colonIdx + 1).trim();
          }
        }
        
        if (!field || field.length < 3 || field.startsWith('---') || field.startsWith('|')) continue;
        // Skip checklist/status rows (numbers, emojis, pending items)
        if (/^\d+$/.test(field) || field.startsWith('⚠') || field.includes('Pending') || field.includes('pending')) continue;
        // Skip prose/status meta-fields (e.g. "Status: BLOCKED — N params REQUIRES INPUT")
        if (FIELD_DENYLIST.some(w => field.toLowerCase().includes(w))) continue;

        // Skip if data already exists for this field
        const fieldLower = field.toLowerCase();
        const alreadyHasData = Array.from(existingData).some(e => fieldLower.includes(e) || e.includes(fieldLower));
        if (alreadyHasData) continue;
        
        const key = fieldLower;
        if (!seenFields.has(key)) {
          seenFields.add(key);
          inputs.push({ field, context, line: i });
        }
      }
      return inputs;
    };

    // Known fields with dropdown options
    // NOTE: order matters — more specific keys (e.g. 'vpc cidr') must come before
    // generic ones (e.g. 'vpc') so extractRequiredInputs matches the right control.
    const FIELD_OPTIONS: Record<string, { label: string; value: string }[]> = {
      'vpc cidr': [{label:'10.0.0.0/16',value:'10.0.0.0/16'},{label:'10.1.0.0/16',value:'10.1.0.0/16'},{label:'172.16.0.0/16',value:'172.16.0.0/16'},{label:'192.168.0.0/16',value:'192.168.0.0/16'}],
      'target account': [{label:'Enter AWS Account ID',value:''}],
      'account id': [{label:'Enter AWS Account ID',value:''}],
      'domain': [{label:'None',value:'none'},{label:'Custom Domain',value:'custom'}],
      'business unit': [{label:'Media',value:'media'},{label:'Telecom',value:'telecom'},{label:'Consumers',value:'consumers'},{label:'Service',value:'service'}],
      'target region': regionOpts,
      'region': regionOpts,
      // Strategy-constrained options — the single source is workflow/strategyOptions.ts.
      'target compute': computeOpts,
      'compute': computeOpts,
      'target database': databaseOpts,
      'database': databaseOpts,
      'data classification': [{label:'Public',value:'public'},{label:'Internal',value:'internal'},{label:'Confidential',value:'confidential'},{label:'Restricted',value:'restricted'}],
      'classification': [{label:'Public',value:'public'},{label:'Internal',value:'internal'},{label:'Confidential',value:'confidential'},{label:'Restricted',value:'restricted'}],
      'architecture': [{label:'Monolith',value:'monolith'},{label:'Microservices',value:'microservices'},{label:'SOA',value:'soa'},{label:'Serverless',value:'serverless'}],
      'deployment strategy': [{label:'Blue/Green',value:'blue_green'},{label:'Rolling',value:'rolling'},{label:'Canary',value:'canary'},{label:'In-place',value:'in_place'}],
      'deployment': [{label:'Blue/Green',value:'blue_green'},{label:'Rolling',value:'rolling'},{label:'Canary',value:'canary'},{label:'Manual',value:'manual'},{label:'CI/CD',value:'ci-cd'}],
      'multi-region': [{label:'No',value:'no'},{label:'Active/Passive',value:'active_passive'},{label:'Active/Active',value:'active_active'}],
      'multi region': [{label:'No',value:'no'},{label:'Active/Passive',value:'active_passive'},{label:'Active/Active',value:'active_active'}],
      'encryption': [{label:'At rest & in transit',value:'both'},{label:'At rest only',value:'at_rest'},{label:'In transit only',value:'in_transit'},{label:'None',value:'none'}],
      'tier': [{label:'Tier 1 — Critical',value:'tier_1'},{label:'Tier 2 — Important',value:'tier_2'},{label:'Tier 3 — Low',value:'tier_3'}],
      'strategy': [{label:'Rehost',value:'rehost'},{label:'Replatform',value:'replatform'},{label:'Refactor',value:'refactor'},{label:'Repurchase',value:'repurchase'},{label:'Retain',value:'retain'},{label:'Retire',value:'retire'}],
      'migration strategy': [{label:'Rehost',value:'rehost'},{label:'Replatform',value:'replatform'},{label:'Refactor',value:'refactor'},{label:'Repurchase',value:'repurchase'},{label:'Retain',value:'retain'},{label:'Retire',value:'retire'}],
      'containerization': [{label:'Already Containerized',value:'containerized'},{label:'Planned - Docker',value:'planned_docker'},{label:'Planned - Kubernetes',value:'planned_k8s'},{label:'Not Planned',value:'not_planned'},{label:'N/A',value:'na'}],
      'ci/cd': [{label:'AWS CodePipeline',value:'codepipeline'}],
      'pipeline': [{label:'AWS CodePipeline',value:'codepipeline'}],
      'environment': [{label:'Production',value:'prod'},{label:'Staging',value:'staging'},{label:'Development',value:'dev'},{label:'UAT',value:'uat'}],
      'vpc': [{label:'Create New VPC',value:'new'},{label:'Use Existing VPC',value:'existing'}],
      'load balancer': [{label:'ALB (Application)',value:'alb'},{label:'NLB (Network)',value:'nlb'},{label:'None',value:'none'}],
      'dns': [{label:'Route 53',value:'route53'},{label:'External DNS',value:'external'},{label:'None',value:'none'}],
      'monitoring': [{label:'CloudWatch',value:'cloudwatch'},{label:'Datadog',value:'datadog'},{label:'Prometheus + Grafana',value:'prometheus'},{label:'New Relic',value:'newrelic'}],
      'logging': [{label:'CloudWatch Logs',value:'cloudwatch'},{label:'ELK Stack',value:'elk'},{label:'Datadog',value:'datadog'},{label:'Splunk',value:'splunk'}],
      'target vpc': [{label:'Create New VPC',value:'new'},{label:'Use Existing',value:'existing'}],
      'target subnets': [{label:'Create New',value:'new'},{label:'Use Existing',value:'existing'}],
      'container port': [{label:'8080',value:'8080'},{label:'3000',value:'3000'},{label:'80',value:'80'},{label:'443',value:'443'}],
      'health check': [{label:'/health',value:'/health'},{label:'/actuator/health',value:'/actuator/health'},{label:'/',value:'/'},{label:'/api/health',value:'/api/health'}],
      'ecr': [{label:'Auto-generate',value:'auto'}],
      'log group': [{label:'Auto-generate',value:'auto'}],
    };

    const requiredInputs = designDoc ? extractRequiredInputs(designDoc) : [];

    // Get version number for previous version links
    const currentVer = parseInt((app?.design_version || 'v0').replace('v', '')) || 0;
    const versionLinks = Array.from({ length: currentVer }, (_, i) => `v${i + 1}`);

    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
          <div><Box variant="awsui-key-label">Design Version</Box><Badge color="blue">{app?.design_version || (designDoc ? 'v1' : '—')}</Badge></div>
          <PhaseStatusField />
        </ColumnLayout>

        {/* Confirmed target configuration (from the assessment gate) — shown on the design
            screen so the engineer sees the account/VPC/compute the design was built against. */}
        {(app?.target_compute || app?.target_account || (app?.transformation_steps || []).length > 0) && (
          <Container header={<Header variant="h3">Confirmed Target Configuration</Header>}>
            <ColumnLayout columns={3} variant="text-grid">
              <div><Box variant="awsui-key-label">Target Compute</Box><Badge color={app?.target_compute ? 'blue' : 'grey'}>{app?.target_compute || 'Not recorded'}</Badge></div>
              <div><Box variant="awsui-key-label">Target Database</Box><Badge color={app?.target_database ? 'blue' : 'grey'}>{app?.target_database || 'Not recorded'}</Badge></div>
              <div><Box variant="awsui-key-label">CI/CD Pipeline</Box><Badge color={app?.cicd_target ? 'blue' : 'grey'}>{app?.cicd_target || 'Not recorded'}</Badge></div>
              <div><Box variant="awsui-key-label">AWS Account ID</Box><Badge color={app?.target_account ? 'blue' : 'grey'}>{app?.target_account || 'Not recorded'}</Badge></div>
              <div><Box variant="awsui-key-label">VPC CIDR</Box><Badge color={app?.vpc_cidr ? 'blue' : 'grey'}>{app?.vpc_cidr || 'Not recorded'}</Badge></div>
              <div>
                <Box variant="awsui-key-label">Transformation Units ({(app?.transformation_steps || []).length})</Box>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                  {(app?.transformation_steps || []).map((t: any, i: number) => {
                    const f = typeof t === 'string' ? t : (t?.td_name || t?.name || 'transform');
                    return <Badge key={`${f}-${i}`} color="green">{f.replace('AWS/', '')}</Badge>;
                  })}
                  {(!app?.transformation_steps || app.transformation_steps.length === 0) && <Box color="text-body-secondary">None</Box>}
                </div>
              </div>
            </ColumnLayout>
          </Container>
        )}

        {/* Version links */}
        {versionLinks.length > 1 && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <Box variant="awsui-key-label">Versions:</Box>
            {versionLinks.map(v => (
              <Badge key={v} color={v === app?.design_version ? 'blue' : 'grey'}>{v}</Badge>
            ))}
          </div>
        )}

        {loadingDoc && <StatusIndicator type="loading">Loading design document...</StatusIndicator>}
        {designDoc && (
          <Tabs tabs={[
            { id: 'doc', label: 'Design Document', content: <MarkdownRenderer content={designDoc} /> },
            ...(requiredInputs.length > 0 ? [{ id: 'inputs', label: `Required Inputs (${requiredInputs.length})`, content: (
              <SpaceBetween size="m">
                <Box color="text-body-secondary">The following fields were flagged in the design document. Fill them in and retry to regenerate with your inputs.</Box>
                <Table
                  columnDefinitions={[
                    { id: 'field', header: 'Field', cell: (item: any) => <Box fontWeight="bold">{item.field}</Box>, width: 200 },
                    { id: 'context', header: 'Context', cell: (item: any) => <Box fontSize="body-s" color="text-body-secondary">{item.context}</Box>, width: 300 },
                    { id: 'input', header: 'Your Input', cell: (item: any) => {
                      const fieldLower = item.field.toLowerCase();
                      const matchedKey = Object.keys(FIELD_OPTIONS).find(k => fieldLower.includes(k) || k.includes(fieldLower));
                      if (matchedKey) {
                        return <Select expandToViewport selectedOption={inputValues[item.field] || null} onChange={({detail}) => { updateInputValue(item.field, detail.selectedOption); }} options={FIELD_OPTIONS[matchedKey]} placeholder="Select..." />;
                      }
                      return <Input value={inputValues[item.field] || ''} placeholder="Enter value..." onChange={({detail}) => { updateInputValue(item.field, detail.value); }} />;
                    }},
                  ]}
                  items={requiredInputs}
                  variant="embedded"
                />
              </SpaceBetween>
            )}] : []),
          ]} />
        )}
        {/* Approval is handled by the modal footer (Approve/Retry/Cancel) — no inline buttons here */}
        <TaskDetails />
      </SpaceBetween>
    );
  }
  if (phase === 'assessment') {
    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
          <div><Box variant="awsui-key-label">Strategy</Box><Badge color="blue">{app?.migration_strategy ? `${app.migration_strategy.charAt(0).toUpperCase()}${app.migration_strategy.slice(1)}${app?.strategy_confidence != null ? ` · ${app.strategy_confidence}%` : ''}` : 'Not recorded'}</Badge></div>
          <PhaseStatusField />
        </ColumnLayout>
        {/* Approved scope shows once the assessment gate is passed. Hide it only while the
            engineer is actively at the assessment gate (the editable form is shown there);
            keep showing it at every later step (design_review, etc.) — not just when no gate
            is open. Missing target fields remain explicit rather than an ambiguous dash. */}
        {wf?.current_step !== 'assessment_review' && (app?.target_compute || (app?.transformation_steps && app.transformation_steps.length > 0)) && (
          <Container header={<Header variant="h3">Approved Transformation Scope</Header>}>
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Target Compute</Box>
                <Badge color={app?.target_compute ? 'blue' : 'grey'}>{app?.target_compute || 'Not recorded'}</Badge>
              </div>
              <div>
                <Box variant="awsui-key-label">CI/CD Pipeline</Box>
                <Badge color={app?.cicd_target ? 'blue' : 'grey'}>{app?.cicd_target || 'Not recorded'}</Badge>
              </div>
              <div>
                <Box variant="awsui-key-label">Target Database</Box>
                <Badge color={app?.target_database ? 'blue' : 'grey'}>{app?.target_database || 'Not recorded'}</Badge>
              </div>
              <div>
                <Box variant="awsui-key-label">AWS Account ID</Box>
                <Badge color={app?.target_account ? 'blue' : 'grey'}>{app?.target_account || 'Not recorded'}</Badge>
              </div>
              <div>
                <Box variant="awsui-key-label">VPC CIDR</Box>
                <Badge color={app?.vpc_cidr ? 'blue' : 'grey'}>{app?.vpc_cidr || 'Not recorded'}</Badge>
              </div>
              <div>
                <Box variant="awsui-key-label">Transformation Units ({(app?.transformation_steps || []).length})</Box>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                  {(app?.transformation_steps || []).map((t: any, i: number) => {
                    const unit = typeof t === 'object' ? t?.unit : undefined;
                    const fallback = typeof t === 'string' ? t : (t?.td_name || t?.name || 'Unknown transform');
                    const label = unit ? `${unit} · ${TRANSFORMATION_UNIT_LABELS[unit] || fallback.replace('AWS/', '')}` : fallback.replace('AWS/', '');
                    return <Badge key={`${unit || fallback}-${i}`} color="green">{label}</Badge>;
                  })}
                  {(!app?.transformation_steps || app.transformation_steps.length === 0) && <Box color="text-body-secondary">None selected</Box>}
                </div>
              </div>
            </ColumnLayout>
          </Container>
        )}
        <Container header={<Header variant="h3">Assessment Reports</Header>}>
          <AssessmentReportTabs appId={appId} />
        </Container>
        {/* Approval is handled by the modal footer (Approve/Retry/Cancel) — no inline buttons here */}
        <TaskDetails />
      </SpaceBetween>
    );
  }

  // Migration Plan phase — show plan + inputs
  if (phase === 'migration_plan') {
    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
          <div><Box variant="awsui-key-label">Strategy</Box><Badge color="blue">{app?.migration_strategy || '—'}</Badge></div>
          <PhaseStatusField />
        </ColumnLayout>
        <MigrationPlanContent appId={appId} />
        <TaskDetails />
      </SpaceBetween>
    );
  }

  // Wave Plan phase — show wave plan report
  if (phase === 'planning') {
    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
          <div><Box variant="awsui-key-label">Wave</Box><Badge color="blue">{app?.wave_number ? `Wave ${app.wave_number}` : '—'}</Badge></div>
          <PhaseStatusField />
        </ColumnLayout>
        <WavePlanReport appId={appId} />
        <TaskDetails />
      </SpaceBetween>
    );
  }

  // Default — generic phase info
  return (
    <SpaceBetween size="m">
      <ColumnLayout columns={3} variant="text-grid">
        <div><Box variant="awsui-key-label">Application</Box><Box fontWeight="bold">{app?.app_name || appId}</Box></div>
        <div><Box variant="awsui-key-label">Strategy</Box><Badge color="blue">{app?.migration_strategy || '—'}</Badge></div>
        <div><Box variant="awsui-key-label">Current Phase</Box><Badge>{wf?.current_phase || phase}</Badge></div>
      </ColumnLayout>

      <ColumnLayout columns={3} variant="text-grid">
        <PhaseStatusField />
        <div><Box variant="awsui-key-label">Started</Box><Box fontSize="body-s">{wf?.started_at ? new Date(wf?.started_at).toLocaleString() : '—'}</Box></div>
        <div><Box variant="awsui-key-label">Waiting Since</Box><Box fontSize="body-s">{wf?.waiting_since ? new Date(wf.waiting_since).toLocaleString() : '—'}</Box></div>
      </ColumnLayout>

      {/* Phase-specific info */}
      {(phase === 'transform' || phase === 'infrastructure' || phase === 'execution') && (
        <ExecutionReportContent appId={appId} app={app} wf={wf} region={caps.aws_region} />
      )}
      {/* Back at the deploy gate after a deploy/validation: the deployment + latest verdict only;
          the full results live in the Validation step (its column opens once a run exists). */}
      {phase === 'execution' && (app?.exec_deploy_status === 'deployed' || app?.validation_status) && (
        <ValidationResults view="deploy" appId={appId} deployStatus={app?.exec_deploy_status} compute={app?.target_compute} inProgress={wf?.current_step === 'validation_running'} />
      )}

      {phase === 'validation' && (
        <ValidationResults appId={appId} deployStatus={app?.exec_deploy_status} compute={app?.target_compute} inProgress={wf?.current_step === 'validation_running'} />
      )}

      {phase === 'cutover' && (
        <SpaceBetween size="m">
          <Alert type="info">Production cutover / go-live. Review the traffic-shift and rollback plan, then Go/No-Go.</Alert>
          <Container header={<Header variant="h3">Cutover Plan</Header>}>
            <ColumnLayout columns={2} variant="text-grid">
              <div><Box variant="awsui-key-label">Strategy</Box><Box>Blue/Green via CodeDeploy</Box></div>
              <div><Box variant="awsui-key-label">Target</Box><Box>ECS Fargate{(app?.current_region || caps.aws_region) ? ` (${app?.current_region || caps.aws_region})` : ''}</Box></div>
              <div><Box variant="awsui-key-label">Traffic Shift</Box><Box>10% → 50% → 100% (5 min intervals)</Box></div>
              <div><Box variant="awsui-key-label">Rollback Trigger</Box><Box>Error rate &gt; 5% or p99 &gt; 500ms</Box></div>
              <div><Box variant="awsui-key-label">Health Check</Box><StatusIndicator type="success">All endpoints healthy</StatusIndicator></div>
              <div><Box variant="awsui-key-label">DNS Cutover</Box><Box>{appId}.app.example.com → new ALB</Box></div>
            </ColumnLayout>
          </Container>
        </SpaceBetween>
      )}

      {phase === 'handover' && (
        <HandoverContent appId={appId} strategy={app?.migration_strategy || 'replatform'} app={app} />
      )}

      {app?.runtime && (
        <Container header={<Header variant="h3">Application Context</Header>}>
          <ColumnLayout columns={4} variant="text-grid">
            <div><Box variant="awsui-key-label">Language</Box><Box>{app.language}</Box></div>
            <div><Box variant="awsui-key-label">Framework</Box><Box>{app.framework}</Box></div>
            <div><Box variant="awsui-key-label">Architecture</Box><Box>{app.architecture}</Box></div>
            <div><Box variant="awsui-key-label">Tier</Box><Badge color={app.tier === 'tier_1' ? 'red' : 'blue'}>{app.tier}</Badge></div>
          </ColumnLayout>
        </Container>
      )}
      <TaskDetails />
    </SpaceBetween>
  );
};

// ── Inline App Edit Modal ──────────────────────────────────────────────

const AppEditModalInline: React.FC<{ appId: string; onClose: () => void; onSaved: () => void }> = ({ appId, onClose, onSaved }) => {
  const caps = useCapabilities();
  const [ed, setEd] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    setLoading(true);
    apiClient.get(`/portfolio/apps/${appId}`).then((r: any) => {
      const d = (r as any)?.data ?? r;
      setEd(d);
    }).catch(() => setMsg('Failed to load')).finally(() => setLoading(false));
  }, [appId]);

  const save = async () => {
    if (!ed) return;
    setSaving(true); setMsg('');
    try {
      await apiClient.put(`/portfolio/apps/${ed.app_id}`, ed);
      onSaved();
    } catch (e: any) { setMsg(`Save failed: ${e?.message || e}`); }
    setSaving(false);
  };

  // Bind a readiness questionnaire answer (business_readiness / team_readiness map).
  const setReadiness = (group: 'business_readiness' | 'team_readiness', key: string, value: string) =>
    setEd({ ...ed, [group]: { ...(ed?.[group] || {}), [key]: value } });

  if (loading) return <Modal visible onDismiss={onClose} header="Loading..." size="max"><StatusIndicator type="loading">Loading application...</StatusIndicator></Modal>;

  return (
    <Modal visible onDismiss={onClose} header={ed?.app_name || appId} size="max"
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" onClick={save} loading={saving}>Save Changes</Button>
      </SpaceBetween></Box>}>
      {msg && <Alert type={msg.includes('fail') ? 'error' : 'success'} dismissible onDismiss={() => setMsg('')}>{msg}</Alert>}
      {ed && (
        <Tabs tabs={[
          { id: 'overview', label: 'Overview', content: (
            <SpaceBetween size="m">
              <ColumnLayout columns={4}>
                <FormField label="App ID"><Input value={ed.app_id} disabled /></FormField>
                <FormField label="App Name"><Input value={ed.app_name || ''} onChange={({detail})=>setEd({...ed,app_name:detail.value})} /></FormField>
                <FormField label="Tier"><Select selectedOption={{label:ed.tier,value:ed.tier}} onChange={({detail})=>setEd({...ed,tier:detail.selectedOption.value})} options={[{label:'Tier 1',value:'tier_1'},{label:'Tier 2',value:'tier_2'},{label:'Tier 3',value:'tier_3'}]} /></FormField>
                <FormField label="Status"><Select selectedOption={{label:ed.status,value:ed.status}} onChange={({detail})=>setEd({...ed,status:detail.selectedOption.value})} options={['discovered','assessed','planned','migrating','completed'].map(s=>({label:s,value:s}))} /></FormField>
              </ColumnLayout>
              <FormField label="Description"><Textarea value={ed.description || ''} onChange={({detail})=>setEd({...ed,description:detail.value})} rows={2} /></FormField>
              <ColumnLayout columns={4}>
                <FormField label="Business Unit"><Select selectedOption={ed.business_unit?{label:ed.business_unit,value:ed.business_unit}:null} onChange={({detail})=>setEd({...ed,business_unit:detail.selectedOption.value})} options={[{label:'Media',value:'media'},{label:'Telecom',value:'telecom'},{label:'Consumers',value:'consumers'},{label:'Service',value:'service'}]} placeholder="Select..." /></FormField>
                <FormField label="Language"><Input value={ed.language || ''} onChange={({detail})=>setEd({...ed,language:detail.value})} /></FormField>
                <FormField label="Framework"><Input value={ed.framework || ''} onChange={({detail})=>setEd({...ed,framework:detail.value})} /></FormField>
                <FormField label="Architecture"><Select selectedOption={{label:ed.architecture||'monolith',value:ed.architecture||'monolith'}} onChange={({detail})=>setEd({...ed,architecture:detail.selectedOption.value})} options={[{label:'Monolith',value:'monolith'},{label:'Microservices',value:'microservices'},{label:'SOA',value:'soa'},{label:'Serverless',value:'serverless'}]} /></FormField>
              </ColumnLayout>
              <ColumnLayout columns={4}>
                <FormField label="Migration Strategy"><Select selectedOption={ed.migration_strategy?{label:ed.migration_strategy,value:ed.migration_strategy}:null} onChange={({detail})=>setEd({...ed,migration_strategy:detail.selectedOption.value})} options={[{label:'Rehost',value:'rehost'},{label:'Replatform',value:'replatform'},{label:'Refactor',value:'refactor'},{label:'Repurchase',value:'repurchase'},{label:'Retain',value:'retain'},{label:'Retire',value:'retire'}]} placeholder="Select..." /></FormField>
                <FormField label="Deployment"><Input value={ed.deployment_method || ''} onChange={({detail})=>setEd({...ed,deployment_method:detail.value})} /></FormField>
                <FormField label="User Count"><Input type="number" value={String(ed.user_count||0)} onChange={({detail})=>setEd({...ed,user_count:parseInt(detail.value)||0})} /></FormField>
                <FormField label="Data Volume (GB)"><Input type="number" value={String(ed.data_volume_gb||0)} onChange={({detail})=>setEd({...ed,data_volume_gb:parseInt(detail.value)||0})} /></FormField>
              </ColumnLayout>
            </SpaceBetween>
          )},
          { id: 'infra', label: 'Infrastructure', content: (
            <SpaceBetween size="m">
              <Header variant="h3">Runtime</Header>
              <ColumnLayout columns={3}>
                <FormField label="OS"><Input value={ed.runtime?.os || ''} onChange={({detail})=>setEd({...ed,runtime:{...ed.runtime,os:detail.value}})} /></FormField>
                <FormField label="App Server"><Input value={ed.runtime?.app_server || ''} onChange={({detail})=>setEd({...ed,runtime:{...ed.runtime,app_server:detail.value}})} /></FormField>
                <FormField label="Runtime Version"><Input value={ed.runtime?.jdk_version || ed.runtime?.python_version || ''} onChange={({detail})=>setEd({...ed,runtime:{...ed.runtime,jdk_version:detail.value}})} /></FormField>
              </ColumnLayout>
              <Header variant="h3">Database</Header>
              <ColumnLayout columns={4}>
                <FormField label="Engine"><Input value={ed.database?.engine || ''} onChange={({detail})=>setEd({...ed,database:{...ed.database,engine:detail.value}})} /></FormField>
                <FormField label="Version"><Input value={ed.database?.version || ''} onChange={({detail})=>setEd({...ed,database:{...ed.database,version:detail.value}})} /></FormField>
                <FormField label="Size (GB)"><Input type="number" value={String(ed.database?.size_gb||0)} onChange={({detail})=>setEd({...ed,database:{...ed.database,size_gb:parseInt(detail.value)||0}})} /></FormField>
                <FormField label="Licensing"><Input value={ed.database?.licensing || ''} onChange={({detail})=>setEd({...ed,database:{...ed.database,licensing:detail.value}})} /></FormField>
              </ColumnLayout>
              <Header variant="h3">Performance</Header>
              <ColumnLayout columns={3}>
                <FormField label="Peak TPS"><Input type="number" value={String(ed.performance?.peak_tps||0)} onChange={({detail})=>setEd({...ed,performance:{...ed.performance,peak_tps:parseInt(detail.value)||0}})} /></FormField>
                <FormField label="Latency SLA (ms)"><Input type="number" value={String(ed.performance?.latency_sla_ms||0)} onChange={({detail})=>setEd({...ed,performance:{...ed.performance,latency_sla_ms:parseInt(detail.value)||0}})} /></FormField>
                <FormField label="Uptime SLA"><Input value={ed.performance?.uptime_sla || ''} onChange={({detail})=>setEd({...ed,performance:{...ed.performance,uptime_sla:detail.value}})} /></FormField>
              </ColumnLayout>
            </SpaceBetween>
          )},
          { id: 'deps', label: 'Dependencies', content: (
            <SpaceBetween size="m">
              <FormField label="Upstream (comma-separated)"><Input value={(ed.dependencies?.upstream||[]).join(', ')} onChange={({detail})=>setEd({...ed,dependencies:{...ed.dependencies,upstream:detail.value.split(',').map((s:string)=>s.trim()).filter(Boolean)}})} /></FormField>
              <FormField label="Downstream (comma-separated)"><Input value={(ed.dependencies?.downstream||[]).join(', ')} onChange={({detail})=>setEd({...ed,dependencies:{...ed.dependencies,downstream:detail.value.split(',').map((s:string)=>s.trim()).filter(Boolean)}})} /></FormField>
              <FormField label="Shared Database"><Select selectedOption={{label:ed.dependencies?.shared_db?'Yes':'No',value:ed.dependencies?.shared_db?'yes':'no'}} onChange={({detail})=>setEd({...ed,dependencies:{...ed.dependencies,shared_db:detail.selectedOption.value==='yes'}})} options={[{label:'No',value:'no'},{label:'Yes',value:'yes'}]} /></FormField>
            </SpaceBetween>
          )},
          { id: 'cost', label: 'Cost & Constraints', content: (
            <SpaceBetween size="m">
              <ColumnLayout columns={2}>
                <FormField label="Monthly Infra ($)"><Input type="number" value={String(ed.current_cost?.monthly_infra||0)} onChange={({detail})=>setEd({...ed,current_cost:{...ed.current_cost,monthly_infra:parseInt(detail.value)||0}})} /></FormField>
                <FormField label="Annual License ($)"><Input type="number" value={String(ed.current_cost?.annual_license||0)} onChange={({detail})=>setEd({...ed,current_cost:{...ed.current_cost,annual_license:parseInt(detail.value)||0}})} /></FormField>
              </ColumnLayout>
              <ColumnLayout columns={3}>
                <FormField label="DC Lease End"><Input value={ed.constraints?.dc_lease_end || ''} onChange={({detail})=>setEd({...ed,constraints:{...ed.constraints,dc_lease_end:detail.value}})} placeholder="YYYY-MM-DD" /></FormField>
                <FormField label="EOSL Date"><Input value={ed.constraints?.eosl_date || ''} onChange={({detail})=>setEd({...ed,constraints:{...ed.constraints,eosl_date:detail.value}})} placeholder="YYYY-MM-DD" /></FormField>
                <FormField label="Hard Deadline"><Input value={ed.constraints?.hard_deadline || ''} onChange={({detail})=>setEd({...ed,constraints:{...ed.constraints,hard_deadline:detail.value}})} placeholder="YYYY-MM-DD" /></FormField>
              </ColumnLayout>
            </SpaceBetween>
          )},
          { id: 'target', label: 'Current State', content: (
            <SpaceBetween size="m">
              <ColumnLayout columns={4}>
                <FormField label="Current Account"><Input value={ed.current_account || ''} onChange={({detail})=>setEd({...ed,current_account:detail.value})} /></FormField>
                <FormField label="Current Region"><Select selectedOption={ed.current_region?{label:ed.current_region,value:ed.current_region}:null} onChange={({detail})=>setEd({...ed,current_region:detail.selectedOption.value})} options={[...targetRegionOptions(caps), {label:'On-Premises',value:'on-premises'}]} placeholder="Select..." /></FormField>
                <FormField label="Current Compute"><Select selectedOption={ed.current_compute?{label:ed.current_compute,value:ed.current_compute}:null} onChange={({detail})=>setEd({...ed,current_compute:detail.selectedOption.value})} options={[{label:'Bare Metal',value:'bare_metal'},{label:'VMware',value:'vmware'},{label:'EC2',value:'ec2'},{label:'ECS',value:'ecs'},{label:'EKS',value:'eks'}]} placeholder="Select..." /></FormField>
                <FormField label="Containerization"><Select selectedOption={ed.containerization?{label:ed.containerization,value:ed.containerization}:null} onChange={({detail})=>setEd({...ed,containerization:detail.selectedOption.value})} options={[{label:'Already Containerized',value:'containerized'},{label:'Planned Docker',value:'planned_docker'},{label:'Not Planned',value:'not_planned'}]} placeholder="Select..." /></FormField>
              </ColumnLayout>
              <ColumnLayout columns={3}>
                <FormField label="CI/CD"><Select selectedOption={ed.cicd_target?{label:ed.cicd_target,value:ed.cicd_target}:null} onChange={({detail})=>setEd({...ed,cicd_target:detail.selectedOption.value})} options={cicdOptionsFor(ed.migration_strategy)} placeholder="Select..." /></FormField>
                <FormField label="Data Residency"><Input value={ed.data_residency || ''} onChange={({detail})=>setEd({...ed,data_residency:detail.value})} placeholder="e.g. US only" /></FormField>
                <FormField label="Encryption"><Select selectedOption={{label:ed.encryption_required||'yes',value:ed.encryption_required||'yes'}} onChange={({detail})=>setEd({...ed,encryption_required:detail.selectedOption.value})} options={[{label:'At rest & in transit',value:'yes'},{label:'At rest only',value:'at_rest'},{label:'No',value:'no'}]} /></FormField>
              </ColumnLayout>
              <Header variant="h3">DR & Availability</Header>
              <ColumnLayout columns={4}>
                <FormField label="RPO"><Input value={ed.rpo || ''} onChange={({detail})=>setEd({...ed,rpo:detail.value})} placeholder="e.g. 1 hour" /></FormField>
                <FormField label="RTO"><Input value={ed.rto || ''} onChange={({detail})=>setEd({...ed,rto:detail.value})} placeholder="e.g. 4 hours" /></FormField>
                <FormField label="Multi-Region"><Select selectedOption={{label:ed.multi_region||'no',value:ed.multi_region||'no'}} onChange={({detail})=>setEd({...ed,multi_region:detail.selectedOption.value})} options={[{label:'No',value:'no'},{label:'Active/Passive',value:'active_passive'},{label:'Active/Active',value:'active_active'}]} /></FormField>
                <FormField label="Data Classification"><Select selectedOption={ed.data_classification?{label:ed.data_classification,value:ed.data_classification}:null} onChange={({detail})=>setEd({...ed,data_classification:detail.selectedOption.value})} options={[{label:'Public',value:'public'},{label:'Internal',value:'internal'},{label:'Confidential',value:'confidential'},{label:'Restricted',value:'restricted'}]} placeholder="Select..." /></FormField>
              </ColumnLayout>
              <ColumnLayout columns={3}>
                <FormField label="Blackout Periods"><Input value={ed.blackout_periods || ''} onChange={({detail})=>setEd({...ed,blackout_periods:detail.value})} placeholder="e.g. Nov-Dec" /></FormField>
                <FormField label="Hard Deadline"><Input value={ed.hard_deadline || ''} onChange={({detail})=>setEd({...ed,hard_deadline:detail.value})} placeholder="YYYY-MM-DD" /></FormField>
                <FormField label="Budget Constraint"><Input value={ed.budget_constraint || ''} onChange={({detail})=>setEd({...ed,budget_constraint:detail.value})} placeholder="e.g. $50K" /></FormField>
              </ColumnLayout>
              <FormField label="Compliance (comma-separated)"><Input value={(ed.compliance||[]).join(', ')} onChange={({detail})=>setEd({...ed,compliance:detail.value.split(',').map((s:string)=>s.trim()).filter(Boolean)})} placeholder="SOC2, HIPAA, PCI-DSS" /></FormField>
            </SpaceBetween>
          )},
          { id: 'risks', label: `Risks (${ed.risks?.length || 0})`, content: (
            <SpaceBetween size="m">
              {(ed.risks||[]).map((r: string, i: number) => (
                <div key={i} style={{display:'flex',alignItems:'center',gap:8}}>
                  <div style={{flex:1,padding:'6px 10px',borderRadius:4,border:'1px solid #f89256',backgroundColor:'#fff8f0',fontSize:13}}>⚠️ {r}</div>
                  <Button variant="icon" iconName="remove" onClick={()=>setEd({...ed,risks:(ed.risks||[]).filter((_:any,idx:number)=>idx!==i)})} />
                </div>
              ))}
              {(!ed.risks || ed.risks.length === 0) && <Box color="text-body-secondary">No risks identified.</Box>}
              <SpaceBetween size="s" direction="horizontal">
                <Input value="" placeholder="Add a risk..." onChange={({detail})=>{ if(detail.value) setEd({...ed,risks:[...(ed.risks||[]),detail.value]}); }} />
              </SpaceBetween>
            </SpaceBetween>
          )},
          { id: 'assets', label: 'People & Assets', content: (
            <SpaceBetween size="l">
              <Container header={<Header variant="h3">Owners ({(ed.owners||[]).length})</Header>}>
                {(ed.owners||[]).length > 0 && (
                  <Table columnDefinitions={[
                    {id:'n',header:'Name',cell:(o:any)=>o.name},
                    {id:'e',header:'Email',cell:(o:any)=>o.email},
                    {id:'r',header:'Role',cell:(o:any)=><Badge>{(o.role||'').replace(/_/g,' ')}</Badge>},
                    {id:'d',header:'',cell:(o:any)=><Button variant="icon" iconName="remove" onClick={()=>setEd({...ed,owners:(ed.owners||[]).filter((x:any)=>x!==o)})}/>},
                  ]} items={ed.owners||[]} variant="embedded" />
                )}
                {(!ed.owners || ed.owners.length === 0) && <Box color="text-body-secondary">No owners added.</Box>}
                <ColumnLayout columns={4}>
                  <FormField label="Name"><Input value={ed._ownerName||''} onChange={({detail})=>setEd({...ed,_ownerName:detail.value})} placeholder="John Doe" /></FormField>
                  <FormField label="Email"><Input value={ed._ownerEmail||''} onChange={({detail})=>setEd({...ed,_ownerEmail:detail.value})} placeholder="john@example.com" /></FormField>
                  <FormField label="Role"><Select selectedOption={ed._ownerRole?{label:ed._ownerRole,value:ed._ownerRole}:{label:'App Owner',value:'app_owner'}} onChange={({detail})=>setEd({...ed,_ownerRole:detail.selectedOption.value})} options={[{label:'App Owner',value:'app_owner'},{label:'Tech Lead',value:'tech_lead'},{label:'Architect',value:'architect'},{label:'Migration Lead',value:'migration_lead'},{label:'DBA',value:'dba'},{label:'DevOps',value:'devops'}]} /></FormField>
                  <Box padding={{top:'l'}}><Button onClick={()=>{if(ed._ownerName && ed._ownerEmail){setEd({...ed,owners:[...(ed.owners||[]),{name:ed._ownerName,email:ed._ownerEmail,role:ed._ownerRole||'app_owner'}],_ownerName:'',_ownerEmail:'',_ownerRole:''})}}}>Add Owner</Button></Box>
                </ColumnLayout>
              </Container>
              <Container header={<Header variant="h3">Repositories ({(ed.repos||[]).length})</Header>}>
                {(ed.repos||[]).length > 0 && (
                  <Table columnDefinitions={[
                    {id:'n',header:'Name',cell:(r:any)=>r.name || (r.url||'').split('/').pop() || '—'},
                    {id:'u',header:'URL',cell:(r:any)=><a href={r.url} target="_blank" rel="noreferrer">{r.url}</a>},
                    {id:'b',header:'Branch',cell:(r:any)=>r.branch||'main'},
                    {id:'d',header:'',cell:(r:any)=><Button variant="icon" iconName="remove" onClick={()=>setEd({...ed,repos:(ed.repos||[]).filter((x:any)=>x!==r)})}/>},
                  ]} items={ed.repos||[]} variant="embedded" />
                )}
                {(!ed.repos || ed.repos.length === 0) && <Box color="text-body-secondary">No repositories linked.</Box>}
                <ColumnLayout columns={4}>
                  <FormField label="Repo URL"><Input value={ed._repoUrl||''} onChange={({detail})=>setEd({...ed,_repoUrl:detail.value})} placeholder="https://git-codecommit.<region>.amazonaws.com/v1/repos/<name>" /></FormField>
                  <FormField label="Branch"><Input value={ed._repoBranch||''} onChange={({detail})=>setEd({...ed,_repoBranch:detail.value})} placeholder="main" /></FormField>
                  <FormField label="Type"><Select selectedOption={ed._repoType?{label:ed._repoType,value:ed._repoType}:{label:'Application',value:'application'}} onChange={({detail})=>setEd({...ed,_repoType:detail.selectedOption.value})} options={[{label:'Application',value:'application'},{label:'Frontend',value:'frontend'},{label:'Infrastructure',value:'infrastructure'},{label:'Config',value:'config'}]} /></FormField>
                  <Box padding={{top:'l'}}><Button onClick={()=>{const url=(ed._repoUrl||'').trim();if(url){setEd({...ed,repos:[...(ed.repos||[]),{url,branch:(ed._repoBranch||'').trim()||'main',type:ed._repoType||'application',name:url.replace(/\/+$/,'').split('/').pop()?.replace(/\.git$/,'')}],_repoUrl:'',_repoBranch:'',_repoType:''})}}}>Add Repo</Button></Box>
                </ColumnLayout>
              </Container>
              <Container header={<Header variant="h3" description="The generated CodeBuild pipeline fails below this line coverage. Empty = platform default; 0 disables the gate and needs a reason. Takes effect when CI/CD is (re)generated.">CI coverage gate</Header>}>
                <ColumnLayout columns={2}>
                  <FormField label="Minimum line coverage (%)" constraintText="0–100, empty = platform default">
                    <Input type="number" inputMode="numeric" value={ed.coverage_threshold == null ? '' : String(ed.coverage_threshold)}
                      onChange={({detail})=>setEd({...ed,coverage_threshold:detail.value === '' ? null : detail.value})} placeholder="platform default" />
                  </FormField>
                  <FormField label="Reason" constraintText="Why this app differs from the default (recorded in the pipeline notes)">
                    <Input value={ed.coverage_threshold_reason||''} onChange={({detail})=>setEd({...ed,coverage_threshold_reason:detail.value})} />
                  </FormField>
                </ColumnLayout>
              </Container>
              <Container header={<Header variant="h3" description="For relocate/rehost with no source code — a prebuilt Docker image (ECR/JFrog) or executable/binary to deploy as-is.">Deliverables ({(ed.deliverables||[]).length})</Header>}>
                {(ed.deliverables||[]).length > 0 && (
                  <Table columnDefinitions={[
                    {id:'t',header:'Type',cell:(d:any)=><Badge>{d.type==='executable'?'Executable':'Docker Image'}</Badge>},
                    {id:'r',header:'Reference (image URL / exe path)',cell:(d:any)=>d.ref},
                    {id:'n',header:'Note',cell:(d:any)=>d.note||'—'},
                    {id:'x',header:'',cell:(d:any)=><Button variant="icon" iconName="remove" onClick={()=>setEd({...ed,deliverables:(ed.deliverables||[]).filter((x:any)=>x!==d)})}/>},
                  ]} items={ed.deliverables||[]} variant="embedded" />
                )}
                {(!ed.deliverables || ed.deliverables.length === 0) && <Box color="text-body-secondary">No deliverables added.</Box>}
                <ColumnLayout columns={4}>
                  <FormField label="Type"><Select selectedOption={ed._delivType?{label:ed._delivType==='executable'?'Executable':'Docker Image',value:ed._delivType}:{label:'Docker Image',value:'docker_image'}} onChange={({detail})=>setEd({...ed,_delivType:detail.selectedOption.value})} options={[{label:'Docker Image',value:'docker_image'},{label:'Executable',value:'executable'}]} /></FormField>
                  <FormField label="Reference"><Input value={ed._delivRef||''} onChange={({detail})=>setEd({...ed,_delivRef:detail.value})} placeholder="123.dkr.ecr.us-east-1.amazonaws.com/app:tag or /opt/app/run.exe" /></FormField>
                  <FormField label="Note (optional)"><Input value={ed._delivNote||''} onChange={({detail})=>setEd({...ed,_delivNote:detail.value})} placeholder="e.g. vendor-supplied build" /></FormField>
                  <Box padding={{top:'l'}}><Button onClick={()=>{if(ed._delivRef){setEd({...ed,deliverables:[...(ed.deliverables||[]),{type:ed._delivType||'docker_image',ref:ed._delivRef,note:ed._delivNote||''}],_delivType:'',_delivRef:'',_delivNote:''})}}}>Add Deliverable</Button></Box>
                </ColumnLayout>
              </Container>
              <Container header={<Header variant="h3">Machines ({(ed.machines||[]).length})</Header>}>
                {(ed.machines||[]).length > 0 && (
                  <Table columnDefinitions={[
                    {id:'h',header:'Hostname',cell:(m:any)=>m.hostname||m.machine_id},
                    {id:'ip',header:'IP',cell:(m:any)=>m.ip_address||'—'},
                    {id:'os',header:'OS',cell:(m:any)=>m.os_name||'—'},
                    {id:'c',header:'CPU',cell:(m:any)=>`${m.cpu_cores||0} cores`},
                    {id:'m',header:'RAM',cell:(m:any)=>`${m.memory_gb||0} GB`},
                    {id:'d',header:'',cell:(m:any)=><Button variant="icon" iconName="remove" onClick={()=>{
                      const newMachines = (ed.machines||[]).filter((x:any)=>x.machine_id!==m.machine_id);
                      const newIds = (ed.machine_ids||[]).filter((id:string)=>id!==m.machine_id);
                      setEd({...ed, machines:newMachines, machine_ids:newIds, machine_count:newIds.length});
                    }}/>},
                  ]} items={ed.machines||[]} variant="embedded" />
                )}
                {(!ed.machines || ed.machines.length === 0) && <Box color="text-body-secondary">No machines linked.</Box>}
              </Container>
              <Container header={<Header variant="h3">Integrations ({(ed.integrations||[]).length})</Header>}>
                {(ed.integrations||[]).length > 0 && (
                  <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>{(ed.integrations||[]).map((t:string,i:number)=>(
                    <Badge key={i}>{t} <span style={{cursor:'pointer'}} onClick={()=>setEd({...ed,integrations:(ed.integrations||[]).filter((_:any,idx:number)=>idx!==i)})}>✕</span></Badge>
                  ))}</div>
                )}
                {(!ed.integrations || ed.integrations.length === 0) && <Box color="text-body-secondary">No integrations.</Box>}
                <ColumnLayout columns={2}>
                  <FormField label="Add Integration"><Input value={ed._integration||''} onChange={({detail})=>setEd({...ed,_integration:detail.value})} placeholder="e.g. SAP, Kafka, Redis" /></FormField>
                  <Box padding={{top:'l'}}><Button onClick={()=>{if(ed._integration){setEd({...ed,integrations:[...(ed.integrations||[]),ed._integration],_integration:''})}}}>Add</Button></Box>
                </ColumnLayout>
              </Container>
            </SpaceBetween>
          )},
          { id: 'docs', label: 'Documents', content: (
            <SpaceBetween size="m">
              {(ed.documents||[]).length > 0 && (
                <Table columnDefinitions={[
                  {id:'t',header:'Title',cell:(d:any)=>d.title},
                  {id:'u',header:'URL',cell:(d:any)=><a href={d.url} target="_blank" rel="noreferrer">{d.url}</a>},
                  {id:'type',header:'Type',cell:(d:any)=><Badge>{d.type||'other'}</Badge>},
                  {id:'d',header:'',cell:(d:any)=><Button variant="icon" iconName="remove" onClick={()=>setEd({...ed,documents:(ed.documents||[]).filter((x:any)=>x!==d)})}/>},
                ]} items={ed.documents||[]} variant="embedded" />
              )}
              {(!ed.documents || ed.documents.length === 0) && <Box color="text-body-secondary">No documents linked.</Box>}
              <Header variant="h3">Add Document Link</Header>
              <ColumnLayout columns={3}>
                <FormField label="Title"><Input value={ed._docTitle||''} onChange={({detail})=>setEd({...ed,_docTitle:detail.value})} placeholder="Architecture Overview" /></FormField>
                <FormField label="URL"><Input value={ed._docUrl||''} onChange={({detail})=>setEd({...ed,_docUrl:detail.value})} placeholder="https://confluence.example.com/..." /></FormField>
                <FormField label="Type"><Select selectedOption={ed._docType?{label:ed._docType,value:ed._docType}:{label:'Confluence',value:'confluence'}} onChange={({detail})=>setEd({...ed,_docType:detail.selectedOption.value})} options={[{label:'Confluence',value:'confluence'},{label:'Wiki',value:'wiki'},{label:'SharePoint',value:'sharepoint'},{label:'Jira',value:'jira'},{label:'Google Docs',value:'google_docs'},{label:'Other',value:'other'}]} /></FormField>
              </ColumnLayout>
              <Button onClick={()=>{if(ed._docTitle && ed._docUrl){setEd({...ed,documents:[...(ed.documents||[]),{title:ed._docTitle,url:ed._docUrl,type:ed._docType||'confluence'}],_docTitle:'',_docUrl:'',_docType:''})}}}>Add Document</Button>
            </SpaceBetween>
          )},
          { id: 'business_readiness', label: 'Business Readiness', content: (
            <SpaceBetween size="s">
              <Box color="text-body-secondary" fontSize="body-s">Business/stakeholder migration readiness. Feeds the Migration Risk Score (unanswered = highest risk).</Box>
              {BUSINESS_QUESTIONS.map(([key, lbl]) => (
                <FormField key={key} label={lbl}>
                  <Select
                    selectedOption={ed.business_readiness?.[key] ? { label: ed.business_readiness[key], value: ed.business_readiness[key] } : null}
                    onChange={({ detail }) => setReadiness('business_readiness', key, detail.selectedOption.value as string)}
                    options={STATUS_OPTIONS.map((s) => ({ label: s, value: s }))}
                    placeholder="Select status"
                  />
                </FormField>
              ))}
            </SpaceBetween>
          )},
          { id: 'team_readiness', label: 'Team Readiness', content: (
            <SpaceBetween size="s">
              <Box color="text-body-secondary" fontSize="body-s">Delivery-team migration readiness. Feeds the Migration Risk Score (unanswered = highest risk).</Box>
              {TEAM_QUESTIONS.map(([key, lbl]) => (
                <FormField key={key} label={lbl}>
                  <Select
                    selectedOption={ed.team_readiness?.[key] ? { label: ed.team_readiness[key], value: ed.team_readiness[key] } : null}
                    onChange={({ detail }) => setReadiness('team_readiness', key, detail.selectedOption.value as string)}
                    options={STATUS_OPTIONS.map((s) => ({ label: s, value: s }))}
                    placeholder="Select status"
                  />
                </FormField>
              ))}
            </SpaceBetween>
          )},
          { id: 'notes', label: 'Notes', content: (
            <SpaceBetween size="m">
              <FormField label="Migration Notes"><Textarea value={ed.notes || ''} onChange={({detail})=>setEd({...ed,notes:detail.value})} rows={6} placeholder="Add migration notes, context, decisions..." /></FormField>
              <FormField label="Upload Note File (.md, .txt)" description="Upload a file to append to notes">
                <input type="file" accept=".md,.txt,.doc,.pdf" onChange={async (e)=>{
                  const file = e.target.files?.[0];
                  if(file && (file.name.endsWith('.md') || file.name.endsWith('.txt'))){
                    const text = await file.text();
                    setEd({...ed, notes: (ed.notes||'') + (ed.notes ? '\n\n---\n\n' : '') + `## ${file.name}\n${text}`});
                  }
                }} />
              </FormField>
            </SpaceBetween>
          )},
        ]} />
      )}
    </Modal>
  );
};

export const WorkflowPage: React.FC = () => {
  const caps = useCapabilities();
  const [apps, setApps] = useState<any[]>([]);
  const [pipelineApps, setPipelineApps] = useState<workflowApi.ActiveWorkflow[]>([]);
  const [pipelinePending, setPipelinePending] = useState<Record<string, workflowApi.PendingApproval[]>>({});
  const [actionLoading, setActionLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [alert, setAlert] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Modals
  const [uploadModal, setUploadModal] = useState(false);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [selectedApps, setSelectedApps] = useState<any[]>([]);
  const [batchStrategies, setBatchStrategies] = useState<Record<string, any>>({});
  const [portfolioModal, setPortfolioModal] = useState(false);
  const [selectedForPortfolio, setSelectedForPortfolio] = useState<string[]>([]);
  const [waveConfig, setWaveConfig] = useState({ maxApps: '5', teamSize: '5', duration: '4', pilotSize: '2', depHandling: 'sequential', sharedDb: 'together' });

  const [approvalModal, setApprovalModal] = useState<{ appId: string; step: string; message: string } | null>(null);
  // RBAC affordances (the backend enforces; this just avoids a guaranteed 403).
  const { hasRole: userHasRole } = useRole();
  const canMutate = userHasRole(Role.ENGINEER);
  const mutateHint = canMutate ? undefined : permissionHint(Role.ENGINEER);
  const deployEnabled = !!useCapabilities().deploy_enabled;
  const gateRole = requiredRoleForGate(approvalModal?.step, deployEnabled);
  const canDecideGate = userHasRole(gateRole);
  const [comment, setComment] = useState('');
  const [discoveryStrategyOverride, setDiscoveryStrategyOverride] = useState<any>(null);
  const [discoveryRejectInput, setDiscoveryRejectInput] = useState('');
  const [assessmentCompute, setAssessmentCompute] = useState<any>(null);
  const [assessmentCicd, setAssessmentCicd] = useState<any>(null);
  const [assessmentDatabase, setAssessmentDatabase] = useState<any>(null);
  const [assessmentAccount, setAssessmentAccount] = useState<string>('');
  const [assessmentVpcCidr, setAssessmentVpcCidr] = useState('');
  // Completeness-gate gaps the reviewer fills in on the gate (saved to META; on Retry also
  // appended to the reviewer comment the re-run prompt receives).
  const [gateGapValues, setGateGapValues] = useState<GapValues>({});
  const [assessmentAmiId, setAssessmentAmiId] = useState<string>('');
  // Runtime wiring confirmed at the assessment gate: field → value.
  const [assessmentWiring, setAssessmentWiring] = useState<Record<string, string>>({});
  // Deploy gate: the reviewer must confirm the exact commit being deployed.
  const [deployReviewed, setDeployReviewed] = useState(false);
  const [assessmentDbInstanceClass, setAssessmentDbInstanceClass] = useState<Opt | null>(null);
  const [assessmentTransforms, setAssessmentTransforms] = useState<Record<string, boolean>>({});
  // Live ATX catalog for the assessment checklist (AWS-managed; cached server-side).
  const [liveTransforms, setLiveTransforms] = useState<LiveTransformation[]>([]);
  const [assessmentGaps, setAssessmentGaps] = useState<any[]>([]);
  const [assessmentRecommended, setAssessmentRecommended] = useState<string[]>([]);
  // Approve-time error shown inside the approval modal (next to Approve), not the page alert.
  const [approvalError, setApprovalError] = useState<string | null>(null);
  const [showAssessmentErrors, setShowAssessmentErrors] = useState(false);
  const [editAppId, setEditAppId] = useState<string | null>(null);
  const [failureAppId, setFailureAppId] = useState<string | null>(null);
  const [reportModal, setReportModal] = useState<{ appId: string; phase: string } | null>(null);
  const [showWavePlanModal, setShowWavePlanModal] = useState(false);
  const [showPortfolioReportModal, setShowPortfolioReportModal] = useState(false);
  // Missing Patterns — portfolio-wide code-update requirements with no ATX custom
  // transformation, deduped and tagged with every assessment that needs them.
  const [missingPatterns, setMissingPatterns] = useState<any[]>([]);
  const [patternsModalOpen, setPatternsModalOpen] = useState(false);
  // View-modal filters (match an affected app on all active criteria).
  const [mpApp, setMpApp] = useState<string | null>(null);
  const [mpWave, setMpWave] = useState<string | null>(null);
  const [mpBu, setMpBu] = useState<string | null>(null);
  const [mpLang, setMpLang] = useState<string | null>(null);

  // Pagination
  const [currentPage, setCurrentPage] = useState(1);
  const [filterText, setFilterText] = useState('');
  const pageSize = 10;

  // Load apps
  const loadApps = useCallback(async () => {
    try {
      const r = await api.listApps();
      setApps(Array.isArray((r as any)?.data ?? r) ? (r as any)?.data ?? r : []);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
  }, []);

  // Load pipeline statuses
  const loadPipelineStatus = useCallback(async () => {
    try {
      const active = await workflowApi.getActiveWorkflows();
      setPipelineApps(Array.isArray(active) ? active : []);
      const approvals: Record<string, workflowApi.PendingApproval[]> = {};
      for (const wf of (Array.isArray(active) ? active : [])) {
        approvals[wf.app_id] = wf.pending || [];
      }
      setPipelinePending(approvals);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
  }, []);

  useEffect(() => {
    setLoadError(null);
    loadApps();
    loadPipelineStatus();
    getTransformationGaps()
      .then((r: any) => setMissingPatterns((r?.data?.gaps ?? r?.gaps ?? []) as any[]))
      .catch(() => setMissingPatterns([]));
  }, [loadApps, loadPipelineStatus]);

  // Auto-refresh while any workflow is active (RUNNING or awaiting approval) so phase
  // summaries/reports populate as tools finish (no manual Refresh needed). Polls apps +
  // pipeline every workflow_poll_ms (default 10s); stops when nothing is active and
  // pauses while the tab is hidden (refreshing once when it becomes visible again).
  const workflowPollMs = positiveOr(caps.workflow_poll_ms, DEFAULT_WORKFLOW_POLL_MS);
  const [pageVisible, setPageVisible] = useState(() => typeof document === 'undefined' || !document.hidden);
  useEffect(() => {
    const onVisibility = () => setPageVisible(!document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);
  const anyActive = pipelineApps.some((w) => w.status === 'RUNNING' || (w.pending && w.pending.length > 0));
  const wasVisible = React.useRef(pageVisible);
  useEffect(() => {
    const resumed = pageVisible && !wasVisible.current;
    wasVisible.current = pageVisible;
    if (resumed && anyActive) {
      loadApps();
      loadPipelineStatus();
    }
  }, [pageVisible, anyActive, loadApps, loadPipelineStatus]);
  useEffect(() => {
    if (!anyActive || !pageVisible) return;
    const id = setInterval(() => {
      loadApps();
      loadPipelineStatus();
    }, workflowPollMs);
    return () => clearInterval(id);
  }, [pipelineApps, anyActive, pageVisible, workflowPollMs, loadApps, loadPipelineStatus]);

  // When the assessment-review modal opens, load the canonical transformation
  // decision (same /defaults source the design screen uses) so the checklist is
  // pre-checked from the assessment's `recommended` and the red gaps panel shows
  // the missing-TD `gaps` — instead of client-side heuristics.
  useEffect(() => {
    if (approvalModal?.step !== 'assessment_review' || liveTransforms.length) return;
    listTransformations().then((resp: any) => {
      const items = Array.isArray(resp) ? resp : (resp?.data ?? []);
      if (Array.isArray(items)) setLiveTransforms(items);
    }).catch(() => { /* list unavailable → the known catalog is shown */ });
  }, [approvalModal?.step, liveTransforms.length]);

  useEffect(() => {
    if (approvalModal?.step !== 'assessment_review') return;
    let cancelled = false;
    (async () => {
      try {
        const resp: any = await apiClient.get(`/workflow/migration-plan/${approvalModal.appId}/defaults`);
        const d = resp?.data || resp || {};
        if (cancelled) return;
        setAssessmentRecommended(Array.isArray(d.transformation_steps) ? d.transformation_steps : []);
        setAssessmentGaps(Array.isArray(d.transformation_gaps) ? d.transformation_gaps : []);
      } catch {
        if (!cancelled) { setAssessmentRecommended([]); setAssessmentGaps([]); }
      }
    })();
    return () => { cancelled = true; };
  }, [approvalModal?.step, approvalModal?.appId]);

  const handleRefresh = async () => {
    setRefreshing(true);
    setLoadError(null);
    await Promise.all([loadApps(), loadPipelineStatus()]);
    setRefreshing(false);
  };

  // Upload enrichment
  const handleUpload = async () => {
    if (!uploadFiles.length) return;
    setActionLoading(true);
    try {
      const text = await uploadFiles[0].text();
      const data = JSON.parse(text);
      const payload = Array.isArray(data) ? data : [data];
      await apiClient.post('/portfolio/apps/enrich', payload);
      setAlert({ type: 'success', message: `Enriched ${payload.length} application(s)` });
      setUploadModal(false);
      setUploadFiles([]);
      await loadApps();
    } catch (err: any) {
      setAlert({ type: 'error', message: `Upload failed: ${err?.message || err}` });
    }
    setActionLoading(false);
  };

  // Generate wave plan for selected apps
  const handlePortfolioAssessment = async () => {
    if (!selectedForPortfolio.length) return;
    setActionLoading(true);
    try {
      await apiClient.post('/workflow/wave-plan/generate', {
        app_ids: selectedForPortfolio,
        max_apps_per_wave: parseInt(waveConfig.maxApps),
        team_size: parseInt(waveConfig.teamSize),
        wave_duration_weeks: parseInt(waveConfig.duration),
        pilot_wave_size: parseInt(waveConfig.pilotSize),
        dependency_handling: waveConfig.depHandling,
        shared_db_strategy: waveConfig.sharedDb,
      });
      setAlert({ type: 'success', message: `Wave plan generation started for ${selectedForPortfolio.length} application(s). Portfolio assessment running.` });
      setPortfolioModal(false);
      setSelectedForPortfolio([]);
    } catch (err: any) {
      setAlert({ type: 'error', message: err?.message || 'Failed to generate wave plan' });
    }
    setActionLoading(false);
  };

  // Start workflow for selected apps
  const handleStartWorkflow = async () => {
    if (!selectedApps.length) return;
    setActionLoading(true);
    let started = 0;
    let alreadyRunning = 0;
    let failed = 0;
    for (const app of selectedApps) {
      try {
        const appId = app.app_id;
        // Save a strategy only when the user picked one. Discovery decides it
        // otherwise — a 'replatform' fallback here was written to every app and
        // showed as the run's strategy until Discovery overwrote it (APP-CLI).
        const strategy = batchStrategies[appId]?.value;
        if (strategy) await apiClient.post('/portfolio/apps/enrich', [{ app_id: appId, migration_strategy: strategy }]);
        // Start workflow
        await workflowApi.startWorkflow(appId);
        started++;
      } catch (e: any) {
        // An app that already has an active workflow returns 409 — surface it as a
        // distinct, actionable outcome instead of a silent skip (avoids "started for 0").
        if (e?.status === 409 || e?.code === 'WORKFLOW_ALREADY_RUNNING') alreadyRunning++;
        else failed++;
      }
    }
    const parts = [`${started} started`];
    if (alreadyRunning) parts.push(`${alreadyRunning} already running (cancel it first to restart)`);
    if (failed) parts.push(`${failed} failed`);
    setAlert({ type: started ? 'success' : 'info', message: `Workflow: ${parts.join(' · ')}` });
    setSelectedApps([]);
    setBatchStrategies({});
    await loadPipelineStatus();
    await loadApps();
    setActionLoading(false);
  };

  const handleCancelWorkflow = async () => {
    if (!selectedApps.length) return;
    setActionLoading(true);
    let cancelled = 0;
    for (const app of selectedApps) {
      try {
        await apiClient.post('/workflow/cancel', { app_id: app.app_id, reason: 'Cancelled by user from workflow page' });
        cancelled++;
      } catch { /* skip */ }
    }
    setAlert({ type: 'info', message: `Cancelled ${cancelled} workflow(s)` });
    setSelectedApps([]);
    await loadPipelineStatus();
    await loadApps();
    setActionLoading(false);
  };

  // Submit strategy and start workflow for single app
  // Approve
  const handleApprove = async (approved: boolean) => {
    if (!approvalModal) return;
    setActionLoading(true);
    setApprovalError(null);
    try {
      // For discovery_review: save selected strategy before approving, or require input for reject
      if (approvalModal.step === 'discovery_review') {
        // Always save the strategy (whether user changed it or not)
        const currentApp = apps.find((a: any) => a.app_id === approvalModal.appId);
        const strategyToSave = discoveryStrategyOverride?.value || currentApp?.migration_strategy || 'replatform';
        if (approved) {
          await apiClient.put(`/portfolio/apps/${approvalModal.appId}`, { migration_strategy: strategyToSave });
        }
        if (!approved && !discoveryRejectInput.trim()) {
          setApprovalError('Please provide additional input before rejecting so the discovery agent can improve its recommendation.');
          setActionLoading(false);
          return;
        }
        if (!approved && discoveryRejectInput.trim()) {
          await apiClient.post('/portfolio/apps/enrich', [{ app_id: approvalModal.appId, reprocess_comment: discoveryRejectInput }]);
        }
      }

      // Save assessment compute + transform selections before approving
      if (approvalModal.step === 'assessment_review' && approved) {
        // Values are constrained to the strategy by resolveAssessmentInputs; any
        // remaining errors (e.g. a bad account ID) block here and render inline
        // in the modal instead of round-tripping to the backend validator.
        const app = apps.find((a: any) => a.app_id === approvalModal.appId);
        const resolved = resolveAssessmentInputs(app, {
          compute: assessmentCompute, database: assessmentDatabase, cicd: assessmentCicd,
          account: assessmentAccount, vpcCidr: assessmentVpcCidr,
          amiId: assessmentAmiId, dbInstanceClass: assessmentDbInstanceClass,
          transforms: assessmentTransforms, recommended: assessmentRecommended,
        });
        if (resolved.errors.length) {
          setShowAssessmentErrors(true);
          setApprovalError(resolved.errors.map((e) => e.message).join(' '));
          setActionLoading(false);
          return;
        }
        await apiClient.put(`/portfolio/apps/${approvalModal.appId}`, resolved.values);
        const wiring = wiringPayload(app, assessmentWiring, resolved.values.target_compute, resolved.values.target_database);
        if (Object.keys(wiring).length > 0) {
          await apiClient.put(`/portfolio/apps/${approvalModal.appId}`, wiring);
        }
      }

      const gaps = gapPayload(gateGapValues);
      if (Object.keys(gaps).length > 0) {
        await apiClient.put(`/portfolio/apps/${approvalModal.appId}`, gaps);
      }

      const appName = apps.find((a: any) => a.app_id === approvalModal.appId)?.app_name || approvalModal.appId;
      const deployCommit = approvalModal.step === 'execution_review' && deployEnabled
        ? (pipelinePending[approvalModal.appId] || []).find((p) => p.step === 'execution_review')?.decision_summary?.head_commit
        : undefined;
      await workflowApi.approveStep(approvalModal.appId, approvalModal.step, approved,
        approved ? '' : retryComment(discoveryRejectInput || comment, gaps),
        approved && deployCommit ? { deploy_commit: deployCommit } : undefined);
      setAlert({ type: 'success', message: `${approved ? `${appName} — Approved, proceeding to next phase` : `${appName} — Retry, agent will re-run with your inputs`}` });
      setApprovalModal(null);
      setComment('');
      setDiscoveryStrategyOverride(null);
      setDiscoveryRejectInput('');
      setAssessmentCompute(null);
      setAssessmentCicd(null);
      setAssessmentDatabase(null);
      setAssessmentAccount('');
      setAssessmentVpcCidr('');
      setAssessmentAmiId('');
      setAssessmentWiring({});
      setDeployReviewed(false);
      setAssessmentDbInstanceClass(null);
      setAssessmentTransforms({});
      setAssessmentGaps([]);
      setAssessmentRecommended([]);
      setApprovalError(null);
      setShowAssessmentErrors(false);
      setGateGapValues({});
      await loadPipelineStatus();
      await loadApps();
    } catch (err: any) {
      const errMsg = err?.message || err?.data?.message || '';
      if (errMsg.includes('WORKFLOW_NOT_RUNNING') || errMsg.includes('Cannot approve')) {
        const cancelAndRerun = window.confirm(
          `A workflow is already completed/cancelled for this application.\n\nDo you want to cancel the old workflow and start a new one?`
        );
        if (cancelAndRerun) {
          try {
            await workflowApi.cancelWorkflow(approvalModal.appId, 'Cancelled to restart from earlier step');
            await workflowApi.startWorkflow(approvalModal.appId);
            setAlert({ type: 'success', message: `Workflow restarted for ${approvalModal.appId}` });
            setApprovalModal(null);
            setComment('');
            await loadPipelineStatus();
          } catch (restartErr: any) {
            setAlert({ type: 'error', message: `Restart failed: ${restartErr?.message || restartErr}` });
          }
        } else {
          setAlert({ type: 'info', message: 'Action cancelled.' });
        }
      } else {
        setApprovalError(errMsg || 'Failed');
      }
    }
    setActionLoading(false);
  };

  // Build pipeline data: merge apps + active workflows + filter
  const pipelineData = apps.map(app => {
    const wf = pipelineApps.find(w => w.app_id === app.app_id);
    const isEnriched = !!(app.business_unit && app.language);
    return { ...app, workflow: wf, isEnriched, pending: pipelinePending[app.app_id] || [] };
  }).filter(item => {
    if (!filterText) return true;
    const search = filterText.toLowerCase();
    return (item.app_name || '').toLowerCase().includes(search)
      || (item.app_id || '').toLowerCase().includes(search)
      || (item.business_unit || '').toLowerCase().includes(search)
      || (item.migration_strategy || '').toLowerCase().includes(search)
      || (`w${item.wave_number || ''}`).toLowerCase().includes(search)
      || (`wave ${item.wave_number || ''}`).toLowerCase().includes(search);
  }).sort((a, b) => {
    // Sort: Review → Running → Failed → Completed → (not started last)
    const getOrder = (item: any) => {
      if (item.pending && item.pending.length > 0) return 0; // review/approval pending
      if (item.workflow && item.workflow.status === 'RUNNING') return 1; // running
      if (item.workflow && item.workflow.status === 'FAILED') return 2; // failed
      if (item.workflow && item.workflow.status === 'COMPLETED') return 3; // completed
      return 4; // not started
    };
    const orderA = getOrder(a);
    const orderB = getOrder(b);
    if (orderA !== orderB) return orderA - orderB;
    // Within same status group, sort by wave number
    const waveA = parseInt(a.wave_number || '99');
    const waveB = parseInt(b.wave_number || '99');
    return waveA - waveB;
  });

  // Live, strategy-constrained assessment inputs for the open approval modal.
  const assessmentResolved = approvalModal?.step === 'assessment_review'
    ? resolveAssessmentInputs(apps.find((a: any) => a.app_id === approvalModal.appId), {
        compute: assessmentCompute, database: assessmentDatabase, cicd: assessmentCicd,
        account: assessmentAccount, vpcCidr: assessmentVpcCidr,
        amiId: assessmentAmiId, dbInstanceClass: assessmentDbInstanceClass,
        transforms: assessmentTransforms, recommended: assessmentRecommended,
      })
    : null;
  const assessmentFieldError = (field: string) =>
    showAssessmentErrors ? assessmentResolved?.errors.find((e) => e.field === field)?.message : undefined;
  const closeApprovalModal = () => {
    setApprovalModal(null); setComment(''); setDiscoveryStrategyOverride(null); setDiscoveryRejectInput('');
    setApprovalError(null); setShowAssessmentErrors(false); setGateGapValues({});
  };

  const paginatedData = pipelineData.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const totalPages = Math.ceil(pipelineData.length / pageSize);

  return (
    <SpaceBetween size="l">
      <Header variant="h1"
        description="Select applications → Start workflow → Agents handle the rest"
        actions={
          <SpaceBetween size="xs" direction="horizontal">
            <Button iconName="refresh" loading={refreshing} onClick={handleRefresh} />
            <span title={mutateHint}><Button iconName="upload" onClick={() => setUploadModal(true)} disabled={DEMO_READ_ONLY || !canMutate}>L2 Discovery</Button></span>
            <span title={mutateHint}><Button variant="primary" onClick={handleStartWorkflow} loading={actionLoading} disabled={DEMO_READ_ONLY || !canMutate || selectedApps.length === 0}>
              ▶ Start Workflow ({selectedApps.length})
            </Button></span>
            <span title={mutateHint}><Button onClick={handleCancelWorkflow} loading={actionLoading} disabled={DEMO_READ_ONLY || !canMutate || selectedApps.length === 0 || !selectedApps.some((a: any) => a.workflow)}>
              ✕ Cancel ({selectedApps.filter((a: any) => a.workflow).length})
            </Button></span>
          </SpaceBetween>
        }
      >
        Workflow
      </Header>

      {DEMO_READ_ONLY && <Alert type="info" header="Read-only demo mode">{DEMO_READ_ONLY_MESSAGE}</Alert>}
      {loadError && <LoadErrorAlert message={loadError} onRetry={handleRefresh} loading={refreshing} />}
      {alert && <Alert type={alert.type} dismissible onDismiss={() => setAlert(null)}>{alert.message}</Alert>}

      {/* Missing Patterns — portfolio-wide code updates with no ATX custom transformation.
          Deduped across every assessment and tagged with each app that needs the same pattern. */}
      {missingPatterns.length > 0 && (() => {
        const bySev = missingPatterns.reduce((acc: Record<string, number>, g: any) => {
          const s = (g.severity || 'medium').toLowerCase();
          acc[s] = (acc[s] || 0) + 1;
          return acc;
        }, {});
        const affectedApps = new Set<string>();
        missingPatterns.forEach((g: any) => (g.apps || []).forEach((a: any) => affectedApps.add(a.app_id)));
        return (
          <div
            role="button"
            tabIndex={0}
            onClick={() => setPatternsModalOpen(true)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setPatternsModalOpen(true); }}
            title="View missing patterns"
            style={{
              cursor: 'pointer', width: 200, height: 200, boxSizing: 'border-box',
              border: '1px solid #d5dbdb', borderRadius: 12, padding: 20, background: '#fff',
              display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
              boxShadow: '0 1px 1px 0 rgba(0,28,36,.15)',
            }}
          >
            <Box variant="awsui-key-label">Missing Patterns</Box>
            <Box fontSize="display-l" fontWeight="bold">{missingPatterns.length}</Box>
            <SpaceBetween direction="horizontal" size="xxs">
              {bySev.high ? <Badge color="red">{bySev.high} High</Badge> : null}
              {bySev.medium ? <Badge color="blue">{bySev.medium} Med</Badge> : null}
              {bySev.low ? <Badge color="grey">{bySev.low} Low</Badge> : null}
            </SpaceBetween>
            <Box fontSize="body-s" color="text-body-secondary">
              across {affectedApps.size} app{affectedApps.size === 1 ? '' : 's'} · click to view
            </Box>
          </div>
        );
      })()}

      <Modal
        visible={patternsModalOpen}
        onDismiss={() => setPatternsModalOpen(false)}
        header={`Missing Patterns (${missingPatterns.length})`}
        size="large"
        footer={<Box float="right"><Button variant="primary" onClick={() => setPatternsModalOpen(false)}>Close</Button></Box>}
      >
        {(() => {
          // Build filter options from every affected app across all gaps.
          const allApps = missingPatterns.flatMap((g: any) => g.apps || []);
          const uniq = (xs: any[]) => Array.from(new Set(xs.filter((v) => v !== null && v !== undefined && v !== ''))).map(String).sort();
          const opt = (label: string, items: string[]) => [{ label: `All ${label}`, value: '' }, ...items.map((v) => ({ label: v, value: v }))];
          const appOpts = opt('applications', uniq(allApps.map((a: any) => a.app_name || a.app_id)));
          const waveOpts = opt('waves', uniq(allApps.map((a: any) => (a.wave_number != null ? `Wave ${a.wave_number}` : ''))));
          const buOpts = opt('BUs', uniq(allApps.map((a: any) => a.business_unit)));
          const langOpts = opt('languages', uniq(allApps.map((a: any) => a.language)));

          const appMatch = (a: any) =>
            (!mpApp || (a.app_name || a.app_id) === mpApp) &&
            (!mpWave || (a.wave_number != null ? `Wave ${a.wave_number}` : '') === mpWave) &&
            (!mpBu || a.business_unit === mpBu) &&
            (!mpLang || a.language === mpLang);
          const anyFilter = !!(mpApp || mpWave || mpBu || mpLang);
          const filtered = missingPatterns
            .map((g: any) => ({ ...g, _apps: anyFilter ? (g.apps || []).filter(appMatch) : (g.apps || []) }))
            .filter((g: any) => !anyFilter || g._apps.length > 0);
          const fmt = (s?: string) => { if (!s) return '—'; const d = new Date(s); return isNaN(d.getTime()) ? s : d.toLocaleString(); };
          const sel = (v: string | null) => (v ? { label: v, value: v } : { label: '', value: '' });

          return (
            <SpaceBetween size="s">
              <Box color="text-body-secondary">
                Code-update requirements found across the portfolio that have no matching ATX custom
                transformation. Each must be authored once and published to the ATX catalog; every
                assessment that needs the same pattern is tagged below, so it is built a single time.
              </Box>
              <ColumnLayout columns={4}>
                <FormField label="Application"><Select selectedOption={sel(mpApp)} onChange={({ detail }) => setMpApp(detail.selectedOption.value || null)} options={appOpts} expandToViewport /></FormField>
                <FormField label="Wave"><Select selectedOption={sel(mpWave)} onChange={({ detail }) => setMpWave(detail.selectedOption.value || null)} options={waveOpts} expandToViewport /></FormField>
                <FormField label="Business Unit"><Select selectedOption={sel(mpBu)} onChange={({ detail }) => setMpBu(detail.selectedOption.value || null)} options={buOpts} expandToViewport /></FormField>
                <FormField label="Language"><Select selectedOption={sel(mpLang)} onChange={({ detail }) => setMpLang(detail.selectedOption.value || null)} options={langOpts} expandToViewport /></FormField>
              </ColumnLayout>
              {anyFilter && (
                <Box>
                  <Button iconName="close" variant="inline-link" onClick={() => { setMpApp(null); setMpWave(null); setMpBu(null); setMpLang(null); }}>
                    Clear filters
                  </Button>
                  <Box variant="span" fontSize="body-s" color="text-body-secondary"> — {filtered.length} of {missingPatterns.length} patterns</Box>
                </Box>
              )}
              {filtered.length === 0 ? (
                <Box textAlign="center" padding="l" color="text-body-secondary">No missing patterns match the filters.</Box>
              ) : filtered.map((g: any, i: number) => (
                <Container key={i}>
                  <SpaceBetween size="xxs">
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                      <Badge color={g.severity === 'high' ? 'red' : 'grey'}>{(g.severity || 'medium').toUpperCase()}</Badge>
                      <Box fontWeight="bold">{g.requirement}</Box>
                    </div>
                    <Box fontSize="body-s" color="text-body-secondary">First seen: {fmt(g.first_seen)}</Box>
                    {g.reason && <Box fontSize="body-s" color="text-body-secondary">{g.reason}</Box>}
                    {Array.isArray(g.scope_samples) && g.scope_samples.length > 0 && (
                      <Box fontSize="body-s" color="text-body-secondary">Scope: {g.scope_samples.join('; ')}</Box>
                    )}
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, paddingTop: 4 }}>
                      {(g._apps || []).map((a: any) => (
                        <Badge key={a.app_id} color="blue">
                          {(a.app_name || a.app_id)}{a.language ? ` · ${a.language}` : ''}{a.wave_number != null ? ` · W${a.wave_number}` : ''}
                        </Badge>
                      ))}
                    </div>
                  </SpaceBetween>
                </Container>
              ))}
            </SpaceBetween>
          );
        })()}
      </Modal>

      {/* Pipeline Status — ALL apps, paginated with search */}
      <Container header={<Header variant="h3">Workflow Status ({pipelineData.length} apps)</Header>}>
        <SpaceBetween size="s">
          <input
            type="text"
            placeholder="Search by app name, ID, or business unit..."
            value={filterText}
            onChange={(e) => { setFilterText(e.target.value); setCurrentPage(1); }}
            style={{ width: '100%', padding: '8px 12px', border: '1px solid #d5dbdb', borderRadius: 4, fontSize: 13 }}
          />
        <Table
          columnDefinitions={[
            { id: 'app', header: 'Application', cell: (item: any) => (
              <a href="#" onClick={(e) => { e.preventDefault(); setEditAppId(item.app_id); }} style={{ color: '#0972d3', cursor: 'pointer', fontWeight: 600, textDecoration: 'none' }}>
                {item.app_name}
              </a>
            ), width: 180 },
            { id: 'strategy', header: 'Strategy', cell: (item: any) => {
              const s = item.migration_strategy || '';
              if (!s) return <Box color="text-body-secondary">—</Box>;
              const colors: Record<string, string> = { retire: 'grey', retain: 'grey', rehost: 'blue', replatform: 'blue', refactor: 'red', repurchase: 'green' };
              return <Badge color={(colors[s] || 'blue') as any}>{s}</Badge>;
            }, width: 95 },
            // Risk tier (persisted on META by the discovery scorer). Mirrors the Risk
            // column semantics via StatusIndicator so color is design-system driven.
            { id: 'risk_tier', header: 'Risk Tier', cell: (item: any) => {
              const t = String(item.risk_tier || '').toLowerCase();
              if (!t) return <Box color="text-body-secondary">—</Box>;
              const type: 'success' | 'warning' | 'error' = t === 'low' ? 'success' : t === 'high' ? 'error' : 'warning';
              return <StatusIndicator type={type}>{item.risk_tier}</StatusIndicator>;
            }, width: 110, sortingField: 'risk_tier' },
            { id: 'machines', header: 'Machines', cell: (item: any) => item.machine_count || 0, width: 75 },
            { id: 'wave', header: 'Wave', cell: (item: any) => item.wave_number ? <Badge color="green">W{item.wave_number}</Badge> : <Box color="text-body-secondary">—</Box>, width: 65 },
            { id: 'bu', header: 'BU', cell: (item: any) => <Badge>{item.business_unit || '—'}</Badge>, width: 80 },
            ...PHASES.map(phase => ({
              id: phase.id,
              header: phase.label,
              width: 110,
              cell: (item: any) => {
                if (!item.workflow) {
                  return <Box color="text-body-secondary">—</Box>;
                }
                const currentIdx = getPhaseIndex(item.workflow.current_phase);
                const phaseIdx = PHASES.findIndex(p => p.id === phase.id);
                
                // Map the pending gate step (e.g. design_review) to its phase column
                const pendingPhase = phaseOf(item.pending?.[0]?.step) || '';
                const hasPending = item.pending.length > 0 && (phaseIdx === currentIdx || phase.id === pendingPhase);

                // Design column: show current version + running indicator if generating next version
                if (phase.id === 'design') {
                  const currentIdx = getPhaseIndex(item.workflow.current_phase);
                  const designIdx = PHASES.findIndex(p => p.id === 'design');
                  const isDesignRunning = currentIdx === designIdx;
                  const hasPendingDesign = item.pending.length > 0 && isDesignRunning;

                  // If pending approval on design phase, show Review — opens the
                  // approval modal (Approve/Retry/Cancel) with the design document.
                  if (hasPendingDesign) {
                    const pendingItem = item.pending[0];
                    return <Button variant="inline-link" onClick={() => {
                      setApprovalModal({ appId: item.app_id, step: pendingItem.step, message: pendingItem.message });
                    }}><Badge color="red">Review {item.design_version || ''}</Badge></Button>;
                  }
                  
                  if (item.design_version && isDesignRunning) {
                    return <Button variant="inline-link" onClick={() => setReportModal({ appId: item.app_id, phase: 'design' })}>{item.design_version}+</Button>;
                  }
                  if (item.design_version) {
                    return <Button variant="inline-link" onClick={() => setReportModal({ appId: item.app_id, phase: 'design' })}>{item.design_version}</Button>;
                  }
                  if (isDesignRunning) {
                    return <Button variant="inline-link" onClick={() => setReportModal({ appId: item.app_id, phase: 'design' })}><StatusIndicator type="in-progress">Running</StatusIndicator></Button>;
                  }
                }

                // Current phase or phase with pending approval — show Review button
                if (hasPending) {
                  const pendingItem = item.pending[0];
                  return <Button variant="inline-link" onClick={() => {
                    setApprovalModal({ appId: item.app_id, step: pendingItem.step, message: pendingItem.message });
                  }}><Badge color="red">Review</Badge></Button>;
                }
                // Completed phase
                if (phaseIdx < currentIdx) {
                  // Close-out strategies (retire/retain/repurchase) skip everything
                  // between Discovery and Handover — including Cutover — per the ASL
                  // (StrategyChoice branch=close_out → Handover_CloseOut).
                  const strategy = item.migration_strategy || '';
                  const isShortPath = ['retire', 'retain', 'repurchase'].includes(strategy);
                  const skippedPhases = ['assessment', 'design', 'execution', 'validation', 'cutover'];
                  if (isShortPath && skippedPhases.includes(phase.id)) {
                    return <Box color="text-body-secondary" fontSize="body-s">Skipped</Box>;
                  }
                  return <Button variant="inline-link" onClick={() => {
                    setReportModal({ appId: item.app_id, phase: phase.id });
                  }}>✓ Done</Button>;
                }
                // Current phase — check if failed or running
                if (phaseIdx === currentIdx) {
                  if (item.workflow.status === 'FAILED') {
                    return <Button variant="inline-link" onClick={() => setFailureAppId(item.app_id)}><StatusIndicator type="error">Failed</StatusIndicator></Button>;
                  }
                  if (item.workflow.status === 'CANCELLED') {
                    return <Box color="text-body-secondary">Cancelled</Box>;
                  }
                  if (item.workflow.status === 'SUCCEEDED' || item.workflow.status === 'COMPLETED') {
                    return <Button variant="inline-link" onClick={() => setReportModal({ appId: item.app_id, phase: phase.id })}>✓ Done</Button>;
                  }
                  return <Button variant="inline-link" onClick={() => setReportModal({ appId: item.app_id, phase: phase.id })}><StatusIndicator type="in-progress">Running</StatusIndicator></Button>;
                }
                // Validation ran but the run is back at the deploy gate: open its results from the Validation column.
                if (phase.id === 'validation' && item.validation_status && phaseIdx === currentIdx + 1) {
                  const v = String(item.validation_status);
                  return <Button variant="inline-link" onClick={() => setReportModal({ appId: item.app_id, phase: 'validation' })}>
                    <StatusIndicator type={v === 'pass' ? 'success' : v === 'fail' ? 'error' : 'warning'}>{v === 'pass_with_warnings' ? 'Warnings' : v}</StatusIndicator></Button>;
                }
                // Future phase
                return <Box color="text-body-secondary">—</Box>;
              },
            })),
          ]}
          items={paginatedData}
          variant="embedded"
          trackBy="app_id"
          selectionType="multi"
          selectedItems={selectedApps}
          onSelectionChange={({ detail }) => setSelectedApps(detail.selectedItems)}
        />
        <Box textAlign="center" margin={{ top: 's' }}>
          <Pagination currentPageIndex={currentPage} pagesCount={totalPages} onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)} />
        </Box>
        </SpaceBetween>
      </Container>


      <FailureReasonModal appId={failureAppId} onClose={() => setFailureAppId(null)} />

      {/* App Edit Modal — inline editable popup */}
      {editAppId && (
        <AppEditModalInline appId={editAppId} onClose={() => setEditAppId(null)} onSaved={() => { setEditAppId(null); loadApps(); }} />
      )}

      {/* Wave Plan Modal */}

            <Modal
        visible={portfolioModal}
        onDismiss={() => { setPortfolioModal(false); setSelectedForPortfolio([]); }}
        header="Generate Wave Plan"
        size="max"
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button onClick={() => { setPortfolioModal(false); setSelectedForPortfolio([]); }}>Cancel</Button>
              <Button variant="primary" onClick={handlePortfolioAssessment} loading={actionLoading} disabled={selectedForPortfolio.length === 0}>
                Generate Wave Plan ({selectedForPortfolio.length} apps)
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Tabs tabs={[
            {
              id: 'apps',
              label: 'Select Applications',
              content: (
                <SpaceBetween size="s">
                  <Box>Select applications with completed assessment or migration plan:</Box>
                  <Table
                    columnDefinitions={[
                      { id: 'name', header: 'Application', cell: (a: any) => a.app_name, width: 160 },
                      { id: 'bu', header: 'BU', cell: (a: any) => <Badge>{a.business_unit}</Badge>, width: 80 },
                      { id: 'strategy', header: 'Strategy', cell: (a: any) => <Badge color="blue">{a.migration_strategy || '—'}</Badge>, width: 100 },
                      { id: 'tier', header: 'Tier', cell: (a: any) => <Badge color={a.tier === 'tier_1' ? 'red' : 'blue'}>{a.tier}</Badge>, width: 70 },
                      { id: 'td', header: 'Portfolio TD', cell: (a: any) => {
                        const s = a.migration_strategy || '';
                        if (['retire', 'retain'].includes(s)) return <Box color="text-body-secondary">Skip</Box>;
                        if (s === 'refactor') return <Box fontSize="body-s">portfolio-agentic-readiness</Box>;
                        return <Box fontSize="body-s">portfolio-modernization-readiness</Box>;
                      }, width: 180 },
                    ]}
                    items={apps.filter(a => a.status === 'assessed' || a.migration_plan_status === 'generated')}
                    selectionType="multi"
                    selectedItems={apps.filter(a => selectedForPortfolio.includes(a.app_id))}
                    onSelectionChange={({ detail }) => setSelectedForPortfolio(detail.selectedItems.map((a: any) => a.app_id))}
                    trackBy="app_id"
                    variant="embedded"
                  />
                </SpaceBetween>
              ),
            },
            {
              id: 'config',
              label: 'Wave Configuration',
              content: (
                <SpaceBetween size="m">
                  <Alert type="info">Configure wave plan parameters. These control how applications are grouped into waves.</Alert>
                  <ColumnLayout columns={2}>
                    <FormField label="Max Applications Per Wave" description="Parallel capacity per wave">
                      <Select expandToViewport selectedOption={{ value: waveConfig.maxApps, label: `${waveConfig.maxApps} apps` }} options={[{ value: '3', label: '3 apps' }, { value: '5', label: '5 apps' }, { value: '8', label: '8 apps' }, { value: '10', label: '10 apps' }]} onChange={({ detail }) => setWaveConfig({ ...waveConfig, maxApps: detail.selectedOption.value || '5' })} />
                    </FormField>
                    <FormField label="Team Size (engineers)" description="Available engineers per wave">
                      <Select expandToViewport selectedOption={{ value: waveConfig.teamSize, label: waveConfig.teamSize }} options={[{ value: '3', label: '3' }, { value: '5', label: '5' }, { value: '8', label: '8' }, { value: '10', label: '10' }, { value: '15', label: '15' }]} onChange={({ detail }) => setWaveConfig({ ...waveConfig, teamSize: detail.selectedOption.value || '5' })} />
                    </FormField>
                    <FormField label="Wave Duration (weeks)" description="Calendar time per wave">
                      <Select expandToViewport selectedOption={{ value: waveConfig.duration, label: `${waveConfig.duration} weeks` }} options={[{ value: '2', label: '2 weeks' }, { value: '4', label: '4 weeks' }, { value: '6', label: '6 weeks' }, { value: '8', label: '8 weeks' }]} onChange={({ detail }) => setWaveConfig({ ...waveConfig, duration: detail.selectedOption.value || '4' })} />
                    </FormField>
                    <FormField label="Pilot Wave Size" description="Apps in Wave 0 (pilot)">
                      <Select expandToViewport selectedOption={{ value: waveConfig.pilotSize, label: `${waveConfig.pilotSize} apps` }} options={[{ value: '1', label: '1 app' }, { value: '2', label: '2 apps' }, { value: '3', label: '3 apps' }]} onChange={({ detail }) => setWaveConfig({ ...waveConfig, pilotSize: detail.selectedOption.value || '2' })} />
                    </FormField>
                    <FormField label="Dependency Handling" description="How to handle app dependencies">
                      <Select expandToViewport selectedOption={{ value: waveConfig.depHandling, label: waveConfig.depHandling === 'sequential' ? 'Sequential (dependent in earlier wave)' : 'Same Wave (migrate together)' }} options={[{ value: 'sequential', label: 'Sequential (dependent in earlier wave)' }, { value: 'same_wave', label: 'Same Wave (migrate together)' }]} onChange={({ detail }) => setWaveConfig({ ...waveConfig, depHandling: detail.selectedOption.value || 'sequential' })} />
                    </FormField>
                    <FormField label="Shared DB Strategy" description="Apps sharing a database">
                      <Select expandToViewport selectedOption={{ value: waveConfig.sharedDb, label: waveConfig.sharedDb === 'together' ? 'Together (same wave)' : 'Last (DB migrates last)' }} options={[{ value: 'together', label: 'Together (same wave)' }, { value: 'last', label: 'Last (DB migrates last)' }]} onChange={({ detail }) => setWaveConfig({ ...waveConfig, sharedDb: detail.selectedOption.value || 'together' })} />
                    </FormField>
                  </ColumnLayout>
                  <FormField label="Priority Order" description="How to rank apps for wave assignment (drag to reorder)">
                    <Box fontSize="body-s">
                      1. Readiness Score (higher = earlier wave)<br/>
                      2. Tier (tier_3 first for pilot, tier_1 last)<br/>
                      3. Dependency Count (fewer = earlier)<br/>
                      4. Foundational Blockers (fewer = earlier)
                    </Box>
                  </FormField>
                </SpaceBetween>
              ),
            },
          ]} />
        </SpaceBetween>
      </Modal>

      {/* Upload Enrichment Modal */}
      <Modal
        visible={uploadModal}
        onDismiss={() => { setUploadModal(false); setUploadFiles([]); }}
        header="L2 Discovery — Upload Enrichment Data"
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button onClick={() => { setUploadModal(false); setUploadFiles([]); }}>Cancel</Button>
              <Button variant="primary" onClick={handleUpload} loading={actionLoading} disabled={!uploadFiles.length}>Upload & Enrich</Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info">Upload a JSON file with application enrichment data (repos, owners, BU, runtime, database, etc.)</Alert>
          <FileUpload
            value={uploadFiles}
            onChange={({ detail }) => setUploadFiles(detail.value as any)}
            accept=".json"
            i18nStrings={{
              uploadButtonText: (e) => e ? 'Choose files' : 'Choose file',
              dropzoneText: (e) => e ? 'Drop files' : 'Drop file',
              removeFileAriaLabel: (e) => `Remove ${e + 1}`,
              limitShowFewer: 'Show fewer', limitShowMore: 'Show more', errorIconAriaLabel: 'Error',
            }}
            constraintText="JSON file"
          />
        </SpaceBetween>
      </Modal>

      {/* Approval Modal — with report tabs for assessment_review */}
      <Modal
        visible={!!approvalModal}
        onDismiss={closeApprovalModal}
        header={STEP_LABELS[approvalModal?.step || ''] || approvalModal?.step}
        size="max"
        footer={
          <SpaceBetween size="s">
          {approvalModal && !canDecideGate ? (
            <Alert type="info" header="View only">
              {approvalModal.step === 'execution_review' && deployEnabled
                ? 'Approving this gate deploys the reviewed commit and requires the Admin role.'
                : `You can review this gate, but approving or retrying it: ${permissionHint(gateRole)}`}
            </Alert>
          ) : null}
          {(approvalError || (showAssessmentErrors && assessmentResolved?.errors.length)) ? (
            <Alert type="error" header="Cannot approve yet">
              {showAssessmentErrors && assessmentResolved?.errors.length
                ? <ul style={{ margin: 0, paddingLeft: 18 }}>{assessmentResolved.errors.map((e) => <li key={e.field}>{e.message}</li>)}</ul>
                : approvalError}
            </Alert>
          ) : null}
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button onClick={closeApprovalModal}>Close</Button>
              <Button onClick={async () => {
                if (!approvalModal || !discoveryRejectInput.trim()) {
                  setAlert({ type: 'error', message: 'Please provide a reason in the Additional Input field before cancelling.' });
                  return;
                }
                setActionLoading(true);
                try {
                  await workflowApi.cancelWorkflow(approvalModal.appId, discoveryRejectInput.trim());
                  setAlert({ type: 'info', message: `Workflow cancelled for ${approvalModal.appId}` });
                  setApprovalModal(null);
                  setDiscoveryRejectInput('');
                  await loadPipelineStatus();
                  await loadApps();
                } catch (e: any) { setAlert({ type: 'error', message: e?.message || 'Cancel failed' }); }
                setActionLoading(false);
              }} disabled={actionLoading || !canMutate || !discoveryRejectInput.trim()}>Cancel Workflow</Button>
              <span title={canDecideGate ? undefined : permissionHint(gateRole)}><Button onClick={() => handleApprove(false)} disabled={actionLoading || !canDecideGate || !(discoveryRejectInput.trim() || Object.keys(gapPayload(gateGapValues)).length)}>Retry</Button></span>
              <span title={canDecideGate ? undefined : permissionHint(gateRole)}><Button variant="primary" onClick={() => handleApprove(true)} loading={actionLoading} disabled={!canDecideGate || (approvalModal?.step === 'execution_review' && deployEnabled && !deployReviewed)}>{approvalModal?.step === 'execution_review' && deployEnabled ? 'Deploy' : 'Approve'}</Button></span>
            </SpaceBetween>
          </Box>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="m">
          <Box fontWeight="bold">{approvalModal?.appId}</Box>
          <Box>{approvalModal?.message}</Box>

          {/* Consistency findings computed by the gate (assessment / design /
              execution decision_summary) — surfaced before the human approves. */}
          {(() => {
            const ds = approvalModal
              ? (pipelinePending[approvalModal.appId] || []).find((p) => p.step === approvalModal.step)?.decision_summary
              : undefined;
            const contradictions = ds?.contradictions || [];
            const warnings = ds?.warnings || [];
            const notes = ds?.notes || [];
            const autoFixable = (ds?.auto_fixable_at_execution as string[] | undefined) || [];
            return (
              <>
                {contradictions.length > 0 && (
                  <Alert type="error" header={`Contradicts the approved selections (${contradictions.length})`}>
                    <ul style={{ margin: 0, paddingLeft: 18 }}>{contradictions.map((c, i) => <li key={i}>{c}</li>)}</ul>
                  </Alert>
                )}
                {warnings.length > 0 && (
                  <Alert type="warning" header={`Review before approving (${warnings.length})`}>
                    <ul style={{ margin: 0, paddingLeft: 18 }}>{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
                  </Alert>
                )}
                {approvalModal?.step === 'execution_review' && !deployEnabled && (
                  <Alert type="info" header="Deploy is disabled">
                    Approving accepts the migration branch{ds?.head_commit ? <> at commit <Box variant="code" display="inline">{ds.head_commit.slice(0, 12)}</Box></> : null}.
                    AMF applies nothing: Validation runs its static checks, and you deploy the committed IaC and pipeline yourself.
                  </Alert>
                )}
                {approvalModal?.step === 'execution_review' && deployEnabled && ds?.head_commit && (
                  <Container header={<Header variant="h3" description="Approving deploys exactly this commit: terraform apply of its infra/, then the first pipeline run, then Validation on the live stack.">Review the migration branch</Header>}>
                    <SpaceBetween size="s">
                      <Box>Branch <Box variant="code" display="inline">{ds.review_branch || '—'}</Box> at commit <Box variant="code" display="inline">{ds.head_commit.slice(0, 12)}</Box>
                        {ds.review_url && <> — <Link external href={ds.review_url}>open in CodeCommit</Link></>}</Box>
                      <Checkbox checked={deployReviewed} onChange={({ detail }) => setDeployReviewed(detail.checked)}>
                        I reviewed the changes on this commit and want to deploy it.
                      </Checkbox>
                    </SpaceBetween>
                  </Container>
                )}
                {autoFixable.length > 0 && (
                  <Alert type="info" header={`CI readiness — auto-fixed on the migration branch at Execution (${autoFixable.length})`}>
                    <ul style={{ margin: 0, paddingLeft: 18 }}>{autoFixable.map((n, i) => <li key={i}>{n}</li>)}</ul>
                  </Alert>
                )}
                {notes.length > 0 && (
                  <Alert type="info" header={`Generated-artifact notes (${notes.length})`}>
                    <ul style={{ margin: 0, paddingLeft: 18 }}>{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
                  </Alert>
                )}
                {completenessGaps(warnings).length > 0 && (
                  <Container header={<Header variant="h3" description="Saved to the application on Approve or Retry. On Retry they are also passed to the agent so the regenerated document uses them.">Provide missing inputs</Header>}>
                    <ColumnLayout columns={2}>
                      {completenessGaps(warnings).map((f) => {
                        const def = GAP_FIELDS[f];
                        const v = gateGapValues[f];
                        return (
                          <FormField key={f} label={def.label} description={def.description}>
                            {def.kind === 'multi' ? (
                              <Multiselect
                                selectedOptions={(def.options || []).filter((o) => Array.isArray(v) && v.includes(o.value))}
                                onChange={({ detail }) => setGateGapValues({ ...gateGapValues, [f]: detail.selectedOptions.map((o) => o.value as string) })}
                                options={def.options || []}
                                placeholder="Select…"
                              />
                            ) : def.kind === 'select' ? (
                              <Select
                                selectedOption={(def.options || []).find((o) => o.value === v) || null}
                                onChange={({ detail }) => setGateGapValues({ ...gateGapValues, [f]: detail.selectedOption.value as string })}
                                options={def.options || []}
                                placeholder="Select…"
                              />
                            ) : (
                              <Input value={typeof v === 'string' ? v : ''} placeholder={def.placeholder}
                                onChange={({ detail }) => setGateGapValues({ ...gateGapValues, [f]: detail.value })} />
                            )}
                          </FormField>
                        );
                      })}
                    </ColumnLayout>
                  </Container>
                )}
              </>
            );
          })()}

          {/* Discovery Review: show strategy recommendation + strategy override + reject input */}
          {approvalModal?.step === 'discovery_review' && (
            <SpaceBetween size="m">
              <ReportContent appId={approvalModal.appId} phase="discovery" apps={apps} pipelineApps={pipelineApps} hideExecutionDetails />
              <ExecutionDetails appId={approvalModal.appId} phase="discovery" pipelineApps={pipelineApps} strategy={apps.find(a => a.app_id === approvalModal.appId)?.migration_strategy} />

              <Container header={<Header variant="h3">Confirm Strategy</Header>}>
                <SpaceBetween size="m">
                  <FormField label="Migration Strategy" description="The recommended strategy is pre-selected. Change it if you disagree with the recommendation.">
                    <Select
                      expandToViewport
                      selectedOption={
                        discoveryStrategyOverride
                        || STRATEGIES.find(s => s.value === apps.find(a => a.app_id === approvalModal.appId)?.migration_strategy)
                        || STRATEGIES[1]
                      }
                      onChange={({ detail }) => setDiscoveryStrategyOverride(detail.selectedOption)}
                      options={STRATEGIES}
                    />
                  </FormField>
                  <FormField label="Additional Input (required for Cancel / Retry)" description="Provide context so the discovery agent can improve, or explain why you're cancelling.">
                    <Textarea value={discoveryRejectInput} onChange={({ detail }) => setDiscoveryRejectInput(detail.value)} rows={3} placeholder="e.g. The database has been migrated to PostgreSQL 15. Also the app now runs on EKS, not VMware..." />
                  </FormField>
                </SpaceBetween>
              </Container>
            </SpaceBetween>
          )}

          {/* Assessment Review: show reports + compute recommendation + transformation checklist */}
          {approvalModal?.step === 'assessment_review' && (() => {
            const app = apps.find(a => a.app_id === approvalModal.appId);
            const r = assessmentResolved!;
            const isLift = !r.transformsAllowed;
            const defaultCompute = r.compute.value;

            // ATX Transform catalog — shared source of truth (see buildTransformCatalog)
            const TRANSFORM_CATALOG = buildTransformCatalog(app, liveTransforms);

            return (
              <SpaceBetween size="l">
                <ReportContent appId={approvalModal.appId} phase="assessment" apps={apps} pipelineApps={pipelineApps} hideExecutionDetails />

                {/* A missing/empty assessment report never blocks approval — e.g. deliverable-based
                    rehost/relocate with no source has no code analysis. Approve stays enabled. */}
                <Alert type="info">
                  Approval is not gated on the assessment report. If no report is available (e.g. a
                  relocate/rehost deliverable with no source code), you can still confirm the target
                  configuration below and approve to proceed.
                </Alert>

                {/* Target Compute, Database & CI/CD Recommendation — show for all strategies */}
                <Container header={<Header variant="h3">Target Configuration</Header>}>
                  <SpaceBetween size="s">
                    <ColumnLayout columns={2}>
                      <FormField label="Target Compute" errorText={assessmentFieldError('target_compute')} description={isLift ? `${r.strategy || 'This strategy'} is lift & shift — compute is limited to EC2.` : "Design and Execution build exactly this compute."}>
                        <Select
                          selectedOption={r.compute}
                          onChange={({ detail }) => setAssessmentCompute(detail.selectedOption as Opt)}
                          options={r.computeOptions}
                        />
                      </FormField>
                      <FormField label="Target Database" errorText={assessmentFieldError('target_database')} description={app?.database?.engine ? `Current: ${app.database.engine} ${app.database.version || ''}` : "Select target database service."}>
                        <Select
                          selectedOption={r.database}
                          onChange={({ detail }) => setAssessmentDatabase(detail.selectedOption as Opt)}
                          options={r.databaseOptions}
                        />
                      </FormField>
                    </ColumnLayout>
                    <ColumnLayout columns={2}>
                      <FormField label="CI/CD Pipeline" errorText={assessmentFieldError('cicd_target')} description="AWS CodePipeline (CodeCommit source) is the only supported target.">
                        <Select
                          disabled={r.cicdOptions.length <= 1}
                          selectedOption={r.cicd}
                          onChange={({ detail }) => setAssessmentCicd(detail.selectedOption as Opt)}
                          options={r.cicdOptions}
                        />
                      </FormField>
                      <Box padding={{ top: 'l' }}>
                        <Box variant="awsui-key-label">Rationale</Box>
                        <Box fontSize="body-s" color="text-body-secondary">
                          {defaultCompute === 'ecs_fargate' && 'Containers on ECS Fargate.'}
                          {defaultCompute === 'lambda' && 'Serverless functions on Lambda.'}
                          {defaultCompute === 'ec2' && 'EC2 virtual machines (launch template + Auto Scaling group).'}
                          {app?.database?.engine && ` DB: ${app.database.engine} → managed AWS equivalent.`}
                        </Box>
                      </Box>
                    </ColumnLayout>
                    <ColumnLayout columns={2}>
                      <FormField label="AWS Account ID" errorText={assessmentFieldError('target_account')} description="12-digit target account for provisioning — required for the design's landing-zone section.">
                        <Input
                          value={assessmentAccount || app?.target_account || ''}
                          onChange={({ detail }) => setAssessmentAccount(detail.value)}
                          placeholder="123456789012"
                          inputMode="numeric"
                        />
                      </FormField>
                      <FormField label="VPC CIDR" errorText={assessmentFieldError('vpc_cidr')}
                        description="Primary CIDR range for the target VPC — a private (RFC1918) /16–/28 network address that does not overlap your network, e.g. 10.20.0.0/16.">
                        <Input
                          value={assessmentVpcCidr || app?.vpc_cidr || ''}
                          onChange={({ detail }) => setAssessmentVpcCidr(detail.value.trim())}
                          placeholder="10.20.0.0/16"
                        />
                      </FormField>
                    </ColumnLayout>
                    {/* Conditional sizing inputs: the generated ec2 / rds Terraform
                        modules cannot plan without them (Execution pre-hook enforces). */}
                    {(r.needsAmi || r.needsDbInstanceClass) && (
                      <ColumnLayout columns={2}>
                        {r.needsAmi && (
                          <FormField label="AMI ID" errorText={assessmentFieldError('ami_id')}
                            description="AMI the EC2 Auto Scaling group launches — the AWS MGN-launched image or your golden AMI, in the target account/region.">
                            <Input
                              value={assessmentAmiId || app?.ami_id || ''}
                              onChange={({ detail }) => setAssessmentAmiId(detail.value.trim())}
                              placeholder="ami-0123456789abcdef0"
                            />
                          </FormField>
                        )}
                        {r.needsDbInstanceClass && (
                          <FormField label="DB Instance Class" errorText={assessmentFieldError('db_instance_class')}
                            description={`Instance classes supported by ${r.database.label}.`}>
                            <Select
                              selectedOption={r.dbInstanceClassOptions.find(o => o.value === r.values.db_instance_class) || null}
                              onChange={({ detail }) => setAssessmentDbInstanceClass(detail.selectedOption as Opt)}
                              options={r.dbInstanceClassOptions}
                              placeholder="Select instance class"
                            />
                          </FormField>
                        )}
                      </ColumnLayout>
                    )}
                    {/* Runtime wiring: what Execution builds the IaC + pipeline from. Pre-filled from
                        the code-analysis repo scan (detected_*); confirmed values are saved to META. */}
                    {wiringFields(r.values.target_compute, r.values.target_database).length > 0 && (
                      <ExpandableSection headerText="Runtime wiring (detected from the code — confirm)" defaultExpanded variant="footer">
                        <ColumnLayout columns={2}>
                          {wiringFields(r.values.target_compute, r.values.target_database).map((f) => {
                            const src = (app?.detected_wiring_sources || {})[f.key];
                            const detected = app?.[`detected_${f.key}`];
                            return (
                              <FormField key={f.key} label={f.label}
                                description={app?.[f.key] ? 'Set on the application.' : detected !== undefined && detected !== ''
                                  ? `Detected${src ? ` at ${src}` : ''}.` : `Not detected — Execution falls back to ${f.fallback}.`}>
                                <Input value={assessmentWiring[f.key] ?? String(wiringDefault(app, f.key) ?? '')}
                                  onChange={({ detail }) => setAssessmentWiring({ ...assessmentWiring, [f.key]: detail.value })}
                                  placeholder={f.fallback} />
                              </FormField>
                            );
                          })}
                        </ColumnLayout>
                      </ExpandableSection>
                    )}
                  </SpaceBetween>
                </Container>

                {/* Transformation Steps — N/A for rehost, checklist for others */}
                {isLift ? (
                  <Container header={<Header variant="h3">Transformation Steps</Header>}>
                    <Box color="text-body-secondary">N/A — {r.strategy || 'this strategy'} does not change code. The application migrates as-is.</Box>
                  </Container>
                ) : (
                  <Container header={<Header variant="h3" description="Pre-checked from the assessment's recommended ATX transformations. Uncheck steps you don't want to run.">Transformation Steps</Header>}>
                    <ColumnLayout columns={3}>
                      {TRANSFORM_CATALOG.map(cat => (
                        <div key={cat.category}>
                          <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{cat.category}</Box>
                          {cat.items.map(item => {
                            const recommended = assessmentRecommended.includes(item.id);
                            const isChecked = assessmentTransforms[item.id] ?? recommended;
                            return (
                            <div key={item.id} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0' }}>
                              <input
                                type="checkbox"
                                checked={isChecked}
                                onChange={(e) => setAssessmentTransforms({ ...assessmentTransforms, [item.id]: e.target.checked })}
                              />
                              <span style={{ fontSize: 12 }}>{item.label}</span>
                              {recommended && <Badge color="blue">Rec</Badge>}
                              {item.configLabel && isChecked && (
                                <select style={{ fontSize: 11, padding: '1px 4px' }} defaultValue={item.configOptions?.[0]}>
                                  {item.configOptions?.map(opt => <option key={opt} value={opt}>{opt}</option>)}
                                </select>
                              )}
                            </div>
                            );
                          })}
                        </div>
                      ))}
                    </ColumnLayout>
                  </Container>
                )}
                {!isLift && <TransformationGapsPanel gaps={normalizeGaps(assessmentGaps)} />}
                <ExecutionDetails appId={approvalModal.appId} phase="assessment" pipelineApps={pipelineApps} strategy={apps.find(a => a.app_id === approvalModal.appId)?.migration_strategy} />
              </SpaceBetween>
            );
          })()}

          {/* Design Review (Approval_Design gate): show design document */}
          {approvalModal?.step === 'design_review' && (
            <ReportContent appId={approvalModal.appId} phase="design" apps={apps} pipelineApps={pipelineApps} />
          )}

          {/* Execution / Validation / Cutover / Handover gates */}
          {approvalModal && !['discovery_review', 'assessment_review', 'design_review'].includes(approvalModal.step) && (
            <ReportContent
              appId={approvalModal.appId}
              phase={
                approvalModal.step === 'validation_review' ? 'validation' :
                approvalModal.step === 'handover_review' ? 'handover' :
                'execution'
              }
              apps={apps}
              pipelineApps={pipelineApps}
            />
          )}

          {/* Additional Input — shown for all steps (needed for Cancel/Retry) */}
          {approvalModal?.step !== 'discovery_review' && (
            <FormField label="Additional Input (required for Cancel / Retry)">
              <Textarea value={discoveryRejectInput} onChange={({ detail }) => setDiscoveryRejectInput(detail.value)} rows={2} placeholder="Provide reason or additional context..." />
            </FormField>
          )}


        </SpaceBetween>
      </Modal>

      {/* Report Modal — read-only phase view. ReportContent is already a complete
          phase screen (heading + summary + report + execution details), so it is
          rendered directly. */}
      <Modal visible={!!reportModal} onDismiss={() => setReportModal(null)} header={`${reportModal?.appId} — ${reportModal?.phase === 'design' ? 'Design Document' : (PHASES.find(p => p.id === reportModal?.phase)?.label || reportModal?.phase || '')}`} size="max">
        {reportModal && (
          <ReportContent appId={reportModal.appId} phase={reportModal.phase} apps={apps} pipelineApps={pipelineApps} />
        )}
      </Modal>

      {/* Full Wave Plan Modal */}
      <FullWavePlanModal visible={showWavePlanModal} onDismiss={() => setShowWavePlanModal(false)} />

      {/* Portfolio Assessment Report Modal */}
      <PortfolioReportModal visible={showPortfolioReportModal} onDismiss={() => setShowPortfolioReportModal(false)} />
    </SpaceBetween>
  );
};

export default WorkflowPage;
