// Validation phase screen: the real results AMF recorded, not sample data.
//   • deploy record   — GET /analysis/{app}/deploy              (commit, pipeline run, stack outputs)
//   • validation runs — GET /analysis/{app}/validation-results  (every run: verdict + L0–L3 checks + evidence)
//   • artifacts       — GET /workflow/report/{app}/{deploy|validation} (presigned links)
//   • re-run          — POST /workflow/validate/{app}            (async; this screen polls for the new run)
import React, { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import Link from '@cloudscape-design/components/link';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';

import { apiClient, apiErrorMessage } from '../../api/client';
import { useCapabilities } from '../../CapabilitiesContext';
import { DEFAULT_VALIDATION_POLL_MAX, DEFAULT_VALIDATION_POLL_MS, positiveOr } from '../../lib/aws';
import {
  TIERS, checkLabel, consoleLinks, diffRuns, indicatorOf, summarize, verdictIndicator, type ValidationCheck,
} from '../../workflow/validationView';

const TIER_HELP: Record<string, string> = {
  'L0 Platform': 'Generated IaC, CI/CD gates and the live stack (compute, database, ALB/API, ingress scope)',
  'L1 Smoke': 'Health path and parameter-free GET routes answer',
  'L2 Contract': 'Cases from the repo’s amf-smoke.json (or OpenAPI)',
  'L3 Data': 'The record a write case created exists in the app’s table (then cleaned up)',
};
// Poll cadence/cap come from capabilities (validation_poll_ms / validation_poll_max);
// defaults 15 s × 60 = 15 min — a run waits up to 8 min for the in-VPC probe.

async function analysisRuns(appId: string, type: string): Promise<any[]> {
  try {
    const resp: any = await apiClient.get(`/analysis/${appId}/${type}`);
    return Array.isArray(resp) ? resp : resp?.data || [];
  } catch {
    return [];
  }
}

async function artifacts(appId: string, phase: string): Promise<{ name: string; url: string; size: number }[]> {
  try {
    const resp: any = await apiClient.get(`/workflow/report/${appId}/${phase}`);
    return (resp?.data || resp)?.files || [];
  } catch {
    return [];
  }
}

const Evidence: React.FC<{ ev: any }> = ({ ev }) => {
  if (!ev) return null;
  if (ev.table) {
    return <Box fontSize="body-s">Table <b>{ev.table}</b>, key <code>{JSON.stringify(ev.key)}</code> — {ev.present ? 'present' : 'NOT found'}
      {ev.attributes?.length ? <> · attributes: {ev.attributes.join(', ')}</> : null}</Box>;
  }
  const req = ev.request || {};
  const res = ev.response || {};
  return (
    <ExpandableSection headerText="Evidence" variant="footer">
      <ColumnLayout columns={2} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Request</Box>
          <Box fontSize="body-s"><code>{req.method} {req.url}</code></Box>
          {req.body && <Box fontSize="body-s"><code>{req.body}</code></Box>}
          <Box variant="awsui-key-label">Expected</Box>
          <Box fontSize="body-s"><code>{JSON.stringify(ev.expect)}</code></Box>
          <Box fontSize="body-s" color="text-body-secondary">{ev.transport}{ev.source ? ` · ${ev.source}` : ''}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Response</Box>
          <Box fontSize="body-s">HTTP <b>{res.status || '—'}</b>{res.elapsed_ms != null ? ` in ${res.elapsed_ms} ms` : ''}</Box>
          {res.error && <Box fontSize="body-s" color="text-status-error">{res.error}</Box>}
          {res.json_keys?.length > 0 && <Box fontSize="body-s">JSON keys: {res.json_keys.join(', ')}</Box>}
          {res.body_excerpt && <Box fontSize="body-s"><code>{res.body_excerpt}</code></Box>}
        </div>
      </ColumnLayout>
    </ExpandableSection>
  );
};

// view="deploy" is the Execution step's slice (back at the deploy gate): the deployment plus a one-line
// latest verdict. The verdict, L0–L3 checks, run history and Re-run live in the Validation step (view="full").
export const ValidationResults: React.FC<{ appId: string; deployStatus?: string; compute?: string; inProgress?: boolean; view?: 'full' | 'deploy' }> = ({ appId, deployStatus, compute, inProgress, view = 'full' }) => {
  const caps: any = useCapabilities();
  const pollMs = positiveOr(caps.validation_poll_ms, DEFAULT_VALIDATION_POLL_MS);
  const pollMax = positiveOr(caps.validation_poll_max, DEFAULT_VALIDATION_POLL_MAX);
  const [runs, setRuns] = useState<any[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [deploy, setDeploy] = useState<any | null>(null);
  const [files, setFiles] = useState<{ name: string; url: string; size: number }[]>([]);
  const [loading, setLoading] = useState(true);
  const [rerun, setRerun] = useState<{ state: 'idle' | 'running' | 'error'; message?: string; since?: string }>({ state: 'idle' });
  const polls = useRef(0);

  const load = useCallback(async () => {
    setLoading(true);
    const [v, d, depFiles, valFiles] = await Promise.all([
      analysisRuns(appId, 'validation-results'), analysisRuns(appId, 'deploy'),
      artifacts(appId, 'deploy'), artifacts(appId, 'validation'),
    ]);
    setRuns(v);
    setDeploy(d[0] || null);
    setFiles([...depFiles, ...valFiles]);
    setLoading(false);
    return v;
  }, [appId]);

  useEffect(() => { load(); }, [load]);

  // While a re-run is in flight, poll until a run newer than the one we started from appears.
  useEffect(() => {
    if (rerun.state !== 'running') return undefined;
    const t = setInterval(async () => {
      polls.current += 1;
      const v = await load();
      if (v[0]?.SK && v[0].SK !== rerun.since) {
        setSelected(v[0].SK);
        setRerun({ state: 'idle' });
      } else if (polls.current >= pollMax) {
        setRerun({ state: 'error', message: `No new run after ${Math.round((pollMs * pollMax) / 60000)} min — check the tool_execution logs.` });
      }
    }, pollMs);
    return () => clearInterval(t);
  }, [rerun, load, pollMs, pollMax]);

  const startRerun = async () => {
    polls.current = 0;
    try {
      await apiClient.post(`/workflow/validate/${appId}`, {});
      setRerun({ state: 'running', since: runs[0]?.SK || '' });
    } catch (err) {
      setRerun({ state: 'error', message: apiErrorMessage(err) });
    }
  };

  const current = runs.find((r) => r.SK === selected) || runs[0];
  const idx = current ? runs.indexOf(current) : -1;
  const previous = idx >= 0 ? runs[idx + 1] : undefined;
  const result = current?.result || {};
  const summary = summarize(result.verdict, result.checks as ValidationCheck[] | undefined);
  const changes = previous ? diffRuns(previous.result?.checks, result.checks) : [];
  const dep = deploy?.result || {};
  const outputs: Record<string, any> = dep.outputs || {};
  const endpoint = outputs.alb_dns || outputs.api_endpoint;
  const links = consoleLinks(caps?.aws_region || '', appId, compute, outputs, dep.pipeline?.execution_id);
  const runLabel = (r: any) => `${r.result?.validated_at ? new Date(r.result.validated_at).toLocaleString() : r.SK} — ${r.result?.verdict || '?'}`;

  return (
    <SpaceBetween size="m">
      <Container
        header={<Header variant="h3"
          actions={<SpaceBetween direction="horizontal" size="xs">
            <Button iconName="refresh" loading={loading} onClick={load}>Refresh</Button>
            {view === 'full' && (
              <Button variant="primary" loading={rerun.state === 'running'} disabled={dep.status !== 'deployed'} onClick={startRerun}>
                Re-run Validation</Button>)}
          </SpaceBetween>}>Deployment</Header>}
      >
        {deploy ? (
          <ColumnLayout columns={4} variant="text-grid">
            <div><Box variant="awsui-key-label">Status</Box>
              <StatusIndicator type={dep.status === 'deployed' ? 'success' : dep.status ? 'error' : 'pending'}>
                {dep.status || deployStatus || 'unknown'}</StatusIndicator></div>
            <div><Box variant="awsui-key-label">Commit</Box><Box variant="code">{String(dep.commit || '').slice(0, 10) || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Pipeline run</Box><Box fontSize="body-s">{dep.pipeline?.execution_id || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Endpoint</Box><Box fontSize="body-s">
              {endpoint ? (String(endpoint).startsWith('http') ? <Link href={String(endpoint)} external>{String(endpoint)}</Link>
                : <Link href={`http://${endpoint}`} external>{String(endpoint)}</Link>) : '—'}</Box></div>
          </ColumnLayout>
        ) : (
          <StatusIndicator type="pending">
            {deployStatus === 'running' ? 'Deploying — Terraform apply, then the first pipeline run' : 'Not deployed yet'}
          </StatusIndicator>
        )}
        {dep.pipeline?.message && dep.status !== 'deployed' && (
          <Box margin={{ top: 's' }}><Alert type="error">{String(dep.pipeline.message).slice(0, 600)}</Alert></Box>
        )}
        {rerun.state === 'running' && <Box margin={{ top: 's' }}><Alert type="info">Validation re-run started — this screen refreshes when the new run lands (up to ~10 min).</Alert></Box>}
        {inProgress && rerun.state !== 'running' && (
          <Box margin={{ top: 's' }}><Alert type="info">Validation is running now (the in-VPC probe can take several minutes) —
            the run below is the previous one until this one lands. Use Refresh.</Alert></Box>)}
        {rerun.state === 'error' && <Box margin={{ top: 's' }}><Alert type="error">{rerun.message}</Alert></Box>}
        {(links.length > 0 || files.length > 0) && (
          <Box margin={{ top: 's' }}>
            <SpaceBetween direction="horizontal" size="m">
              {links.map((l) => <Link key={l.label} href={l.href} external fontSize="body-s">{l.label}</Link>)}
              {files.map((f) => <Link key={f.url} href={f.url} external fontSize="body-s">{f.name}</Link>)}
            </SpaceBetween>
          </Box>
        )}
      </Container>

      {view === 'deploy' ? (current && (
        <Alert type={summary.verdict === 'fail' ? 'error' : summary.verdict === 'pass' ? 'success' : 'warning'}
          header={`Latest validation: ${summary.verdict}`}>
          {result.validated_at ? `${new Date(result.validated_at).toLocaleString()} · ` : ''}
          {summary.failed.length > 0 ? `failed: ${summary.failed.map((c) => c.check).join(', ')}. ` : ''}
          Checks, evidence, run history and Re-run are in the Validation step.
        </Alert>
      )) : !current ? (
        <Alert type="info">
          Validation has not run yet. It starts automatically when the deploy finishes and the first pipeline run
          of the reviewed commit succeeds.
        </Alert>
      ) : (
        <>
          <Container header={<Header variant="h3"
            actions={runs.length > 1 && (
              <Select
                selectedOption={{ value: current.SK, label: runLabel(current) }}
                onChange={({ detail }) => setSelected(String(detail.selectedOption.value))}
                options={runs.map((r) => ({ value: r.SK, label: runLabel(r) }))}
              />)}
            description={runs.length > 1 ? `${runs.length} runs` : undefined}>Verdict</Header>}>
            <ColumnLayout columns={5} variant="text-grid">
              <div><Box variant="awsui-key-label">Result</Box>
                <StatusIndicator type={verdictIndicator(summary.verdict)}>{summary.verdict}</StatusIndicator></div>
              {(['pass', 'fail', 'warn', 'skip'] as const).map((r) => (
                <div key={r}><Box variant="awsui-key-label">{r}</Box><Box fontSize="heading-m">{summary.counts[r] || 0}</Box></div>
              ))}
            </ColumnLayout>
            {summary.failed.length > 0 && (
              <Box margin={{ top: 's' }}>
                <Alert type="error" header={`${summary.failed.length} failed check(s)`}>
                  {summary.failed.slice(0, 6).map((c) => <div key={c.check}><b>{c.check}</b>: {String(c.detail || '').slice(0, 220)}</div>)}
                </Alert>
              </Box>
            )}
            {previous && (
              <Box margin={{ top: 's' }}>
                <ExpandableSection headerText={`Changes vs previous run (${changes.length})`} defaultExpanded={changes.length > 0 && changes.length <= 8}>
                  {changes.length === 0 ? <Box fontSize="body-s">No check changed result.</Box> : changes.map((ch) => (
                    <Box key={ch.check} fontSize="body-s">
                      <b>{ch.check}</b>: {ch.before ? <StatusIndicator type={indicatorOf(ch.before)}>{ch.before}</StatusIndicator> : 'new'}
                      {' → '}{ch.after ? <StatusIndicator type={indicatorOf(ch.after)}>{ch.after}</StatusIndicator> : 'removed'}
                    </Box>
                  ))}
                </ExpandableSection>
              </Box>
            )}
          </Container>

          {TIERS.filter((t) => summary.byTier[t].length > 0).map((tier) => (
            <Table
              key={tier}
              header={<Header variant="h3" counter={`(${summary.byTier[tier].length})`} description={TIER_HELP[tier]}>{tier}</Header>}
              items={summary.byTier[tier]}
              variant="container"
              wrapLines
              columnDefinitions={[
                { id: 'check', header: 'Check', cell: (c) => checkLabel(c.check), width: 220 },
                { id: 'result', header: 'Result', cell: (c) => <StatusIndicator type={indicatorOf(c.result)}>{c.result}</StatusIndicator>, width: 130 },
                { id: 'detail', header: 'Detail', cell: (c: any) => (
                  <SpaceBetween size="xxs">
                    <Box fontSize="body-s">{String(c.detail || '')}</Box>
                    <Evidence ev={c.evidence} />
                  </SpaceBetween>) },
              ]}
            />
          ))}
        </>
      )}
    </SpaceBetween>
  );
};

export default ValidationResults;
