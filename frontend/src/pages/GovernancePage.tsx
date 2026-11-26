import React, { useState, useEffect, useCallback } from 'react';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Badge from '@cloudscape-design/components/badge';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Alert from '@cloudscape-design/components/alert';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import * as gov from '../api/governance';

// ── Types (mirror the /governance payloads) ───────────────────────────

interface Gate {
  gate?: string;
  name?: string;
  step?: string;
  outcome?: string;
  at?: string;
  timestamp?: string;
  band?: string;
  phase?: string;
  execution?: string;
}

// ── Presentation helpers ──────────────────────────────────────────────

const outcomeIndicator = (outcome?: string) => {
  switch (outcome) {
    case 'approved':
      return <StatusIndicator type="success">Approved</StatusIndicator>;
    case 'rejected':
      return <StatusIndicator type="error">Rejected</StatusIndicator>;
    // A cancelled gate means the run was stopped, not that anyone declined the
    // change — showing it as rejected would imply a review that never happened.
    case 'cancelled':
      return <StatusIndicator type="stopped">Run cancelled</StatusIndicator>;
    case 'pending':
      return <StatusIndicator type="in-progress">Awaiting approval</StatusIndicator>;
    default:
      return <StatusIndicator type="pending">{outcome || 'Unknown'}</StatusIndicator>;
  }
};

const shortTime = (iso?: string) => (iso ? iso.replace('T', ' ').slice(0, 19) : '—');

// ── Per-app approval gates (inline, not a navigation away) ────────────

const AppGates: React.FC<{ appId: string }> = ({ appId }) => {
  const [detail, setDetail] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    gov.getGovernance(appId)
      .then((d: any) => live && setDetail(d))
      .catch((e: any) => live && setError(e?.message || 'Could not load approval gates.'));
    return () => { live = false; };
  }, [appId]);

  if (error) return <Alert type="error" header="Could not load gates">{error}</Alert>;
  if (!detail) return <Box padding="s">Loading…</Box>;

  const gates: Gate[] = detail.gates || [];
  return gates.length === 0 ? (
    <Box padding="m" color="text-status-inactive">
      No approval gates recorded for this application yet.
    </Box>
  ) : (
    <Table
      variant="embedded"
      items={gates}
      columnDefinitions={[
        { id: 'gate', header: 'Gate', cell: (g: Gate) => g.gate || g.name || g.step || '—' },
        { id: 'phase', header: 'Lifecycle', cell: (g: Gate) => (g.band || g.phase) ? <Badge>{g.band || g.phase}</Badge> : '—', width: 150 },
        { id: 'outcome', header: 'Outcome', cell: (g: Gate) => outcomeIndicator(g.outcome), width: 190 },
        { id: 'execution', header: 'Execution', cell: (g: Gate) => g.execution || '—', width: 250 },
        { id: 'when', header: 'When', cell: (g: Gate) => shortTime(g.at || g.timestamp), width: 170 },
      ]}
    />
  );
};

// ── Page ──────────────────────────────────────────────────────────────

export const GovernancePage: React.FC = () => {
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    gov.getGovernanceSummary()
      .then((s: any) => setSummary(s))
      .catch((e: any) => setError(e?.message || 'Could not load governance summary.'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const gates = summary?.gates || {};
  const appIds: string[] = summary?.governed_app_ids || [];

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Audit trail of workflow runs and human approval gates, reconstructed from Step Functions execution history."
        actions={<Button iconName="refresh" onClick={load} loading={loading}>Refresh</Button>}
      >
        Governance
      </Header>

      {error && <Alert type="error" header="Could not load governance">{error}</Alert>}
      {/* Surfaced, not hidden: a partial view is worse than a flagged one. */}
      {summary?.note && <Alert type="warning" header="Partial data">{summary.note}</Alert>}

      {summary && (
        <>
          <ColumnLayout columns={4} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Runs</Box>
              <Box fontSize="display-l" fontWeight="bold">{summary.total_runs ?? 0}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Gates approved</Box>
              <Box fontSize="display-l" fontWeight="bold">
                {gates.approved ?? 0}<Box variant="span" color="text-body-secondary"> / {gates.total ?? 0}</Box>
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Awaiting approval</Box>
              <Box fontSize="display-l" fontWeight="bold">{gates.pending ?? 0}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Rejected</Box>
              <Box fontSize="display-l" fontWeight="bold">{gates.rejected ?? 0}</Box>
            </div>
          </ColumnLayout>

          <Container header={<Header variant="h3">Runs by status</Header>}>
            {Object.keys(summary.runs_by_status || {}).length === 0 ? (
              <Box color="text-status-inactive">No workflow runs recorded yet.</Box>
            ) : (
              <SpaceBetween size="xs" direction="horizontal">
                {Object.entries(summary.runs_by_status as Record<string, number>).map(([status, n]) => (
                  <Badge key={status} color={status === 'FAILED' ? 'red' : status === 'SUCCEEDED' ? 'green' : 'blue'}>
                    {status}: {n}
                  </Badge>
                ))}
              </SpaceBetween>
            )}
          </Container>
        </>
      )}

      <Table
        loading={loading}
        loadingText="Loading governed applications"
        items={appIds.map((app_id) => ({ app_id }))}
        header={<Header variant="h3" counter={`(${appIds.length})`}>Applications under governance</Header>}
        empty={
          <Box textAlign="center" padding="l" color="text-status-inactive">
            No application has started a workflow run yet.
          </Box>
        }
        columnDefinitions={[
          { id: 'app', header: 'Application', cell: (r: { app_id: string }) => <Box fontWeight="bold">{r.app_id}</Box> },
          {
            id: 'actions',
            header: '',
            width: 110,
            cell: (r: { app_id: string }) => (
              <Button variant="inline-link" onClick={() => setExpanded(expanded === r.app_id ? null : r.app_id)}>
                {expanded === r.app_id ? 'Hide' : 'Gates'}
              </Button>
            ),
          },
        ]}
      />

      {expanded && (
        <ExpandableSection variant="container" defaultExpanded headerText={`Approval gates — ${expanded}`}>
          <AppGates appId={expanded} />
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
};
