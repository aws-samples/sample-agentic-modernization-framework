// ProgramPage (Requirements 65–68) — portfolio / program-level views:
//   • R65 portfolio dashboard: every app with phase, status, strategy, wave
//   • R66 wave-level progress (e.g. "Wave 3: 8/12 apps completed")
//   • R67 at-risk apps view for the migration lead
//   • R68 burndown / velocity metrics
// Consumes backend/src/api/v1/portfolio_program.py via api/program.ts.
import React, { useState, useEffect, useCallback } from 'react';
import Box from '@cloudscape-design/components/box';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Table from '@cloudscape-design/components/table';
import Badge from '@cloudscape-design/components/badge';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Button from '@cloudscape-design/components/button';
import Link from '@cloudscape-design/components/link';
import BarChart from '@cloudscape-design/components/bar-chart';
import * as api from '../api/program';
import { apiErrorMessage } from '../api/client';
import { LoadErrorAlert } from '../components/LoadErrorAlert';

type StatusType = 'error' | 'success' | 'in-progress' | 'stopped' | 'pending';

function statusIndicator(status: string): StatusType {
  switch (status) {
    case 'Failed': return 'error';
    case 'Done': return 'success';
    case 'Awaiting Approval': return 'stopped';
    case 'Cancelled': return 'stopped';
    default: return 'in-progress';
  }
}

export const ProgramPage: React.FC = () => {
  const [apps, setApps] = useState<api.ProgramApp[]>([]);
  const [waves, setWaves] = useState<api.WaveBucket[]>([]);
  const [atRisk, setAtRisk] = useState<api.AtRiskApp[]>([]);
  const [metrics, setMetrics] = useState<api.ProgramMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [d, w, r, m] = await Promise.all([
        api.getDashboard(),
        api.getWaves(),
        api.getAtRisk(),
        api.getMetrics(),
      ]);
      setApps(d?.applications ?? []);
      setWaves(w?.waves ?? []);
      setAtRisk(r?.at_risk ?? []);
      setMetrics(m ?? null);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <Box textAlign="center" padding="xxl"><StatusIndicator type="loading">Loading program view...</StatusIndicator></Box>;
  }

  const burndownSeries = metrics
    ? [{
        title: 'Remaining apps',
        type: 'line' as const,
        data: [...metrics.burndown]
          .sort((a, b) => b.weeks_ago - a.weeks_ago)
          .map((p) => ({ x: p.weeks_ago === 0 ? 'now' : `-${p.weeks_ago}w`, y: p.remaining })),
      }]
    : [];

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Portfolio-wide program view across all applications and waves"
        actions={<Button iconName="refresh" onClick={load} loading={loading} />}
      >
        Program
      </Header>

      {loadError && <LoadErrorAlert message={loadError} onRetry={load} loading={loading} />}

      {/* KPIs (R68) */}
      {metrics && (
        <ColumnLayout columns={5} variant="text-grid">
          <Container>
            <Box variant="awsui-key-label">Applications</Box>
            <Box variant="awsui-value-large">{metrics.total}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Completed</Box>
            <Box variant="awsui-value-large">{metrics.completed}</Box>
            <Box fontSize="body-s" color="text-body-secondary">{metrics.completion_pct}% of portfolio</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Velocity</Box>
            <Box variant="awsui-value-large">{metrics.velocity_per_week}</Box>
            <Box fontSize="body-s" color="text-body-secondary">apps / week</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">Awaiting Approval</Box>
            <Box variant="awsui-value-large">{metrics.status_counts['Awaiting Approval'] || 0}</Box>
          </Container>
          <Container>
            <Box variant="awsui-key-label">At Risk</Box>
            <Box variant="awsui-value-large">{atRisk.length}</Box>
            <Box fontSize="body-s" color={atRisk.length ? 'text-status-error' : 'text-body-secondary'}>
              {(metrics.status_counts['Failed'] || 0)} failed
            </Box>
          </Container>
        </ColumnLayout>
      )}

      {/* Wave progress (R66) */}
      <Container header={<Header variant="h3">Wave Progress</Header>}>
        {waves.length > 0 ? (
          <SpaceBetween size="m">
            {waves.map((wv) => (
              <div key={wv.label}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                  <Box fontWeight="bold">{wv.label}</Box>
                  <Box fontSize="body-s" color="text-body-secondary">
                    {wv.Done}/{wv.total} completed ({wv.completion_pct}%)
                    {wv['Awaiting Approval'] ? ` · ${wv['Awaiting Approval']} awaiting` : ''}
                    {wv.Failed ? ` · ${wv.Failed} failed` : ''}
                  </Box>
                </div>
                <div style={{ height: 16, backgroundColor: '#f2f3f3', borderRadius: 3, overflow: 'hidden', display: 'flex' }}>
                  <div style={{ height: '100%', width: `${(wv.Done / wv.total) * 100}%`, backgroundColor: '#2ea597' }} />
                  <div style={{ height: '100%', width: `${(wv['Awaiting Approval'] / wv.total) * 100}%`, backgroundColor: '#e07941' }} />
                  <div style={{ height: '100%', width: `${(wv.Running / wv.total) * 100}%`, backgroundColor: '#0972d3' }} />
                  <div style={{ height: '100%', width: `${(wv.Failed / wv.total) * 100}%`, backgroundColor: '#d91515' }} />
                </div>
              </div>
            ))}
          </SpaceBetween>
        ) : (
          <Box textAlign="center" color="text-body-secondary" padding="l">No applications assigned to waves yet</Box>
        )}
      </Container>

      <ColumnLayout columns={2}>
        {/* At-risk (R67) */}
        <Container header={<Header variant="h3" counter={`(${atRisk.length})`}>At Risk</Header>}>
          {atRisk.length > 0 ? (
            <SpaceBetween size="s">
              {atRisk.map((a) => (
                <div key={a.app_id} style={{ padding: '6px 0', borderBottom: '1px solid #f2f3f3' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <Link href={`/workflow?app=${a.app_id}`}>{a.app_name}</Link>
                    <StatusIndicator type={statusIndicator(a.status)}>{a.status}</StatusIndicator>
                  </div>
                  {a.reasons.map((reason, i) => (
                    <Box key={i} fontSize="body-s" color="text-status-error">• {reason}</Box>
                  ))}
                </div>
              ))}
            </SpaceBetween>
          ) : (
            <Box textAlign="center" color="text-body-secondary" padding="l">No at-risk applications</Box>
          )}
        </Container>

        {/* Burndown (R68) */}
        <Container header={<Header variant="h3">Burndown</Header>}>
          {metrics && !metrics.insufficient_data && burndownSeries[0]?.data.length ? (
            <BarChart
              series={burndownSeries as any}
              xTitle="Week"
              yTitle="Remaining applications"
              height={220}
              hideFilter
              hideLegend
              empty={<Box textAlign="center" color="text-body-secondary">No data</Box>}
            />
          ) : (
            <Box textAlign="center" color="text-body-secondary" padding="l">
              {metrics?.insufficient_data
                ? 'Completed apps have no completion timestamps yet — burndown will populate as apps finish.'
                : 'No completion data yet'}
            </Box>
          )}
        </Container>
      </ColumnLayout>

      {/* Portfolio dashboard table (R65) */}
      <Container header={<Header variant="h3" counter={`(${apps.length})`}>All Applications</Header>}>
        <Table
          variant="embedded"
          items={apps}
          trackBy="app_id"
          empty={<Box textAlign="center" color="text-body-secondary" padding="l">No applications</Box>}
          columnDefinitions={[
            { id: 'app', header: 'Application', cell: (a) => <Link href={`/workflow?app=${a.app_id}`}>{a.app_name}</Link>, sortingField: 'app_name' },
            { id: 'bu', header: 'Business Unit', cell: (a) => a.business_unit || '—' },
            { id: 'strategy', header: 'Strategy', cell: (a) => (a.strategy ? <Badge>{a.strategy}</Badge> : '—') },
            { id: 'wave', header: 'Wave', cell: (a) => (a.wave === null || a.wave === '' ? '—' : `Wave ${a.wave}`) },
            { id: 'phase', header: 'Phase', cell: (a) => a.phase || '—' },
            { id: 'status', header: 'Status', cell: (a) => <StatusIndicator type={statusIndicator(a.status)}>{a.status}</StatusIndicator> },
          ]}
        />
      </Container>
    </SpaceBetween>
  );
};

export default ProgramPage;
