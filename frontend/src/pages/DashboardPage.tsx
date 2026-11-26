import React, { useState, useEffect, useCallback } from 'react';
import Box from '@cloudscape-design/components/box';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import PieChart from '@cloudscape-design/components/pie-chart';
import Badge from '@cloudscape-design/components/badge';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Button from '@cloudscape-design/components/button';
import Link from '@cloudscape-design/components/link';
import * as api from '../api/portfolio';
import * as workflowApi from '../api/workflow';
import { apiErrorMessage } from '../api/client';
import { LoadErrorAlert } from '../components/LoadErrorAlert';

/** Lifecycle rank for ordering workflow apps: Review → Running → Failed → Completed. */
const workflowStatusRank = (wf: { pending?: any[]; status?: string }): number => {
  if ((wf.pending?.length ?? 0) > 0) return 0;         // Awaiting Approval / Review
  const s = (wf.status || '').toUpperCase();
  if (s === 'RUNNING') return 1;
  if (s === 'FAILED') return 2;
  return 3;                                            // Completed / Done
};

export const DashboardPage: React.FC = () => {
  const [apps, setApps] = useState<any[]>([]);
  const [activeWorkflows, setActiveWorkflows] = useState<workflowApi.ActiveWorkflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [appsResp, wfResp] = await Promise.all([
        api.listApps(),
        workflowApi.getActiveWorkflows(),
      ]);
      const d = appsResp?.data ?? appsResp;
      setApps(Array.isArray(d) ? d : []);
      setActiveWorkflows(Array.isArray(wfResp) ? wfResp : []);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return <Box textAlign="center" padding="xxl"><StatusIndicator type="loading">Loading dashboard...</StatusIndicator></Box>;
  }

  // Compute metrics
  const totalApps = apps.length;
  const totalMachines = apps.reduce((s, a) => s + (a.machine_count || (a.machine_ids || []).length || 0), 0);
  const totalRepos = apps.reduce((s, a) => s + (a.repo_count || 0), 0);
  const withStrategy = apps.filter(a => a.migration_strategy).length;
  const withConfidence = apps.filter(a => a.strategy_confidence).length;
  const enriched = apps.filter(a => !!(a.business_unit && a.language)).length;

  // Strategy distribution
  const byStrategy: Record<string, number> = {};
  apps.forEach(a => { if (a.migration_strategy) byStrategy[a.migration_strategy] = (byStrategy[a.migration_strategy] || 0) + 1; });

  // Workflow phases
  const running = activeWorkflows.filter(w => w.pending.length === 0).length;
  const pendingApproval = activeWorkflows.filter(w => w.pending.length > 0).length;
  const byPhase: Record<string, number> = {};
  activeWorkflows.forEach(w => { byPhase[w.current_phase] = (byPhase[w.current_phase] || 0) + 1; });

  // Business unit distribution
  const byBU: Record<string, number> = {};
  apps.forEach(a => { const bu = a.business_unit || 'unassigned'; byBU[bu] = (byBU[bu] || 0) + 1; });

  // Tier distribution
  const byTier: Record<string, number> = {};
  apps.forEach(a => { const t = a.tier || 'unassigned'; byTier[t] = (byTier[t] || 0) + 1; });

  // Process progress
  const l1Complete = totalMachines > 0;
  const l2Complete = enriched > 0;
  const discoveryComplete = withConfidence > 0;
  const assessmentComplete = apps.filter(a => a.status === 'assessed').length;
  const designComplete = apps.filter(a => a.design_version).length;

  const strategyData = Object.entries(byStrategy).map(([key, value]) => ({
    title: key.charAt(0).toUpperCase() + key.slice(1),
    value,
  }));

  return (
    <SpaceBetween size="l">
      <Header variant="h1"
        description="AI-powered large-scale migration and modernization"
        actions={<Button iconName="refresh" onClick={load} loading={loading} />}
      >
        Dashboard
      </Header>

      {loadError && <LoadErrorAlert message={loadError} onRetry={load} loading={loading} />}

      {/* Migration Journey Progress */}
      <Container header={<Header variant="h3">Migration Journey</Header>}>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          {[
            { label: 'L1 Discovery', done: l1Complete, detail: `${totalMachines} machines` },
            { label: 'L2 Discovery', done: l2Complete, detail: `${enriched} apps enriched` },
            { label: 'Strategy', done: discoveryComplete, detail: `${withConfidence} apps scored` },
            { label: 'Assessment', done: assessmentComplete > 0, detail: `${assessmentComplete} assessed` },
            { label: 'Design', done: designComplete > 0, detail: `${designComplete} docs generated` },
            { label: 'Execution', done: false, detail: 'Pending' },
          ].map((step, i) => (
            <React.Fragment key={step.label}>
              <div style={{ flex: 1, textAlign: 'center', padding: '12px 8px', borderRadius: 6, backgroundColor: step.done ? '#f0fdf4' : '#f8f8f8', border: `1px solid ${step.done ? '#2ea597' : '#e9ebed'}` }}>
                <Box fontSize="body-s" fontWeight="bold" color={step.done ? 'text-status-success' : 'text-body-secondary'}>{step.done ? '✓' : '○'} {step.label}</Box>
                <Box fontSize="body-s" color="text-body-secondary">{step.detail}</Box>
              </div>
              {i < 5 && <Box color="text-body-secondary">→</Box>}
            </React.Fragment>
          ))}
        </div>
      </Container>

      {/* KPI Cards */}
      <ColumnLayout columns={6} variant="text-grid">
        <Container>
          <Box variant="awsui-key-label">Applications</Box>
          <Box variant="awsui-value-large">{totalApps}</Box>
          <Box fontSize="body-s" color="text-body-secondary">{enriched} enriched ({totalApps > 0 ? Math.round(enriched/totalApps*100) : 0}%)</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Machines</Box>
          <Box variant="awsui-value-large">{totalMachines}</Box>
          <Box fontSize="body-s" color="text-body-secondary">From L1 Discovery</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Repositories</Box>
          <Box variant="awsui-value-large">{totalRepos}</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Strategy Assigned</Box>
          <Box variant="awsui-value-large">{withStrategy}</Box>
          <Box fontSize="body-s" color="text-body-secondary">{withConfidence} agent-validated</Box>
        </Container>
        <Container>
          <Box variant="awsui-key-label">Active Workflows</Box>
          <Box variant="awsui-value-large">{activeWorkflows.length}</Box>
          <Box fontSize="body-s">{running} running · {pendingApproval} awaiting review</Box>
        </Container>
      </ColumnLayout>

      {/* Workflow + Strategy + Tier */}
      <ColumnLayout columns={3}>
        {/* Active Workflows */}
        <Container header={<Header variant="h3" actions={<Link href="/workflow">View all →</Link>}>Workflow Activity</Header>}>
          {activeWorkflows.length > 0 ? (
            <SpaceBetween size="s">
              {[...activeWorkflows].sort((a, b) => workflowStatusRank(a) - workflowStatusRank(b)).slice(0, 5).map(wf => (
                <div key={wf.app_id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0', borderBottom: '1px solid #f2f3f3' }}>
                  <div>
                    <Box fontWeight="bold" fontSize="body-s">{wf.app_id}</Box>
                    <Box fontSize="body-s" color="text-body-secondary">{wf.current_phase}</Box>
                  </div>
                  {wf.pending.length > 0 ? <Badge color="red">Review</Badge> : <StatusIndicator type="in-progress">Running</StatusIndicator>}
                </div>
              ))}
            </SpaceBetween>
          ) : (
            <Box textAlign="center" padding="l" color="text-body-secondary">No active workflows</Box>
          )}
        </Container>

        {/* Strategy Distribution */}
        <Container header={<Header variant="h3">Strategy Distribution</Header>}>
          {strategyData.length > 0 ? (
            <PieChart data={strategyData} size="medium" hideFilter hideLegend={false} empty={<Box>No data</Box>} />
          ) : (
            <Box textAlign="center" color="text-body-secondary" padding="l">No strategies assigned yet</Box>
          )}
        </Container>

        {/* Tier + BU */}
        <Container header={<Header variant="h3">Portfolio Breakdown</Header>}>
          <SpaceBetween size="m">
            <Box variant="awsui-key-label">By Tier</Box>
            <SpaceBetween size="xxs">
              {Object.entries(byTier).filter(([k]) => k !== 'unassigned').sort().map(([tier, count]) => (
                <div key={tier} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Badge color={tier === 'tier_1' ? 'red' : tier === 'tier_2' ? 'blue' : 'grey'}>{tier.replace('_', ' ').toUpperCase()}</Badge>
                  <Box fontWeight="bold">{count}</Box>
                </div>
              ))}
            </SpaceBetween>
            <Box variant="awsui-key-label">By Business Unit</Box>
            <SpaceBetween size="xxs">
              {Object.entries(byBU).filter(([k]) => k !== 'unassigned').sort((a, b) => b[1] - a[1]).map(([bu, count]) => (
                <div key={bu} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <div style={{ width: 80, fontSize: 12, textTransform: 'capitalize' }}>{bu}</div>
                  <div style={{ flex: 1, height: 14, backgroundColor: '#f2f3f3', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${(count / totalApps) * 100}%`, backgroundColor: '#0972d3', borderRadius: 3 }}></div>
                  </div>
                  <div style={{ width: 20, fontSize: 11, fontWeight: 'bold' }}>{count}</div>
                </div>
              ))}
            </SpaceBetween>
          </SpaceBetween>
        </Container>
      </ColumnLayout>

      {/* Quick Actions */}
      <Container header={<Header variant="h3">Getting Started</Header>}>
        <ColumnLayout columns={4}>
          <div style={{ padding: 12, borderRadius: 6, border: '1px solid #e9ebed' }}>
            <Box fontWeight="bold">1. L1 Discovery</Box>
            <Box fontSize="body-s" color="text-body-secondary">Import AWS Application Discovery Service (ADS) infrastructure data</Box>
            <Box padding={{ top: 'xs' }}><Button href="/inventory" variant="link">Go to L1 Discovery →</Button></Box>
          </div>
          <div style={{ padding: 12, borderRadius: 6, border: '1px solid #e9ebed' }}>
            <Box fontWeight="bold">2. L2 Discovery</Box>
            <Box fontSize="body-s" color="text-body-secondary">Upload app enrichment (owners, repos, business context)</Box>
            <Box padding={{ top: 'xs' }}><Button href="/workflow" variant="link">Go to Workflow →</Button></Box>
          </div>
          <div style={{ padding: 12, borderRadius: 6, border: '1px solid #e9ebed' }}>
            <Box fontWeight="bold">3. Start Workflow</Box>
            <Box fontSize="body-s" color="text-body-secondary">Select apps and start the migration workflow</Box>
            <Box padding={{ top: 'xs' }}><Button href="/workflow" variant="link">Go to Workflow →</Button></Box>
          </div>
          <div style={{ padding: 12, borderRadius: 6, border: '1px solid #e9ebed' }}>
            <Box fontWeight="bold">4. Wave Planning</Box>
            <Box fontSize="body-s" color="text-body-secondary">Group assessed apps into migration waves</Box>
            <Box padding={{ top: 'xs' }}><Button href="/wave-planning" variant="link">Go to Wave Planning →</Button></Box>
          </div>
        </ColumnLayout>
      </Container>
    </SpaceBetween>
  );
};

export default DashboardPage;
