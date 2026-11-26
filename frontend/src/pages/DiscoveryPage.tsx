import React, { useState, useEffect, useCallback, useMemo } from 'react';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Header from '@cloudscape-design/components/header';
import Pagination from '@cloudscape-design/components/pagination';
import TextFilter from '@cloudscape-design/components/text-filter';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Badge from '@cloudscape-design/components/badge';
import Modal from '@cloudscape-design/components/modal';
import Tabs from '@cloudscape-design/components/tabs';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Select from '@cloudscape-design/components/select';
import FormField from '@cloudscape-design/components/form-field';
import { useCollection } from '@cloudscape-design/collection-hooks';
import Alert from '@cloudscape-design/components/alert';
import { apiClient, apiErrorMessage } from '../api/client';
import { LoadErrorAlert } from '../components/LoadErrorAlert';
import { uploadDiscoveryExport } from '../api/discovery';
import { DEMO_READ_ONLY, DEMO_READ_ONLY_MESSAGE } from '../demo';

export const DiscoveryPage: React.FC = () => {
  const [machines, setMachines] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<any | null>(null);
  const [sel, setSel] = useState<any[]>([]);
  const [apps, setApps] = useState<any[]>([]);
  const [showAssign, setShowAssign] = useState(false);
  const [assignTarget, setAssignTarget] = useState<any>(null);
  const [assigning, setAssigning] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const [importing, setImporting] = useState(false);
  const [importStatus, setImportStatus] = useState<string>('');
  const [importError, setImportError] = useState<string>('');
  const [importSource, setImportSource] = useState<any>({ value: 'aws_ads', label: 'AWS Application Discovery Service (ADS)' });

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const r = await apiClient.get('/portfolio/machines');
      const d = (r as any)?.data ?? r;
      setMachines(Array.isArray(d) ? d : []);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const runImport = async () => {
    if (!importFiles.length) return;
    setImporting(true);
    setImportError('');
    setImportStatus('Uploading & ingesting…');
    try {
      const resp: any = await uploadDiscoveryExport(importFiles);
      const d = resp?.data ?? resp;
      const source = d?.source || importSource.value || 'aws_ads';
      const summary = `Done! ${d?.machines_created ?? 0} machines, ${d?.applications_inferred ?? 0} app groups, ${d?.connections_stored ?? 0} connections, ${d?.databases_created ?? 0} databases. Source: ${source}`;
      setImportStatus(summary);
      await load();
      setTimeout(() => { setShowImport(false); setImportFiles([]); setImportStatus(''); }, 3000);
    } catch (e: any) {
      setImportError(e?.response?.data?.data?.message || e?.message || 'Upload failed');
    } finally {
      setImporting(false);
    }
  };

  // ── Dashboard metrics ───────────────────────────────────────────────
  const metrics = useMemo(() => {
    if (!machines.length) return null;

    // OS distribution
    const osDist: Record<string, number> = {};
    machines.forEach(m => {
      const os = (m.os_name || 'Unknown').split(' ').slice(0, 2).join(' ');
      osDist[os] = (osDist[os] || 0) + 1;
    });

    // Platform distribution
    const platDist: Record<string, number> = {};
    machines.forEach(m => {
      const p = m.virtualization || 'Unknown';
      platDist[p] = (platDist[p] || 0) + 1;
    });

    // Utilization buckets
    let underUtilized = 0, rightSized = 0, overUtilized = 0, noData = 0;
    machines.forEach(m => {
      const cpu = m.avg_cpu_utilization || 0;
      if (cpu === 0) noData++;
      else if (cpu < 20) underUtilized++;
      else if (cpu > 70) overUtilized++;
      else rightSized++;
    });

    // Total resources
    const totalCPU = machines.reduce((s, m) => s + (m.cpu_cores || 0), 0);
    const totalRAM = machines.reduce((s, m) => s + (m.memory_gb || 0), 0);
    const totalStorage = machines.reduce((s, m) => s + (m.storage_gb || 0), 0);

    // EC2 recommendations distribution
    const ec2Dist: Record<string, number> = {};
    machines.forEach(m => {
      const rec = m.ec2_recommendation || '';
      if (rec) ec2Dist[rec] = (ec2Dist[rec] || 0) + 1;
    });

    // Strategy distribution (when the discovery source suggests one)
    const stratDist: Record<string, number> = {};
    machines.forEach(m => {
      const s = m.suggested_strategy || '';
      if (s) stratDist[s] = (stratDist[s] || 0) + 1;
    });

    return { osDist, platDist, underUtilized, rightSized, overUtilized, noData, totalCPU, totalRAM, totalStorage, ec2Dist, stratDist };
  }, [machines]);

  const { items, filterProps, paginationProps, collectionProps } = useCollection(machines, {
    filtering: {
      empty: <Box textAlign="center"><b>No machines discovered</b></Box>,
      noMatch: <Box textAlign="center"><b>No match</b></Box>,
      filteringFunction: (item: any, text: string) => {
        const t = text.toLowerCase();
        return [item.hostname, item.ip_address, item.os_name, item.environment, item.virtualization]
          .filter(Boolean).some(v => String(v).toLowerCase().includes(t));
      },
    },
    pagination: { pageSize: 15 },
    sorting: {},
  });

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Infrastructure inventory from AWS Application Discovery Service (ADS)"
        actions={
          <Button variant="primary" onClick={() => { setShowImport(true); setImportError(''); setImportStatus(''); }} disabled={DEMO_READ_ONLY}>
            Import Discovery Data
          </Button>
        }
      >
        L1 Discovery
      </Header>

      {DEMO_READ_ONLY && <Alert type="info" header="Read-only demo mode">{DEMO_READ_ONLY_MESSAGE}</Alert>}
      {loadError && <LoadErrorAlert message={loadError} onRetry={load} loading={loading} />}

      <Modal
        visible={showImport}
        onDismiss={() => !importing && setShowImport(false)}
        header="Import Discovery Data"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setShowImport(false)} disabled={importing}>Cancel</Button>
              <Button variant="primary" onClick={runImport} loading={importing} disabled={!importFiles.length}>
                Upload &amp; ingest
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField label="Discovery Source" description="Select the tool used to export the discovery data">
            <Select
              selectedOption={importSource}
              onChange={({ detail }) => setImportSource(detail.selectedOption)}
              options={[
                { value: 'aws_ads', label: 'AWS Application Discovery Service (ADS)', description: 'server_inventory.csv, network_data_full.csv, database_data_full.csv, vmware_data_mpa.csv' },
              ]}
            />
          </FormField>
          <FormField label="Upload Folder" description="Select the ADS export folder (contains server_inventory.csv, network_data_full.csv, etc.)">
            <input
              type="file"
              multiple
              accept=".csv,.json"
              {...{ webkitdirectory: '', directory: '' } as any}
              onChange={(e) => {
                const allFiles = Array.from(e.target.files || []);
                const filtered = allFiles.filter(f => f.name.endsWith('.csv') || f.name.endsWith('.json'));
                setImportFiles(filtered);
              }}
            />
          </FormField>
          {importFiles.length > 0 ? (
            <Box key="files" fontSize="body-s">{importFiles.length} file(s) from folder: {importFiles.slice(0, 8).map(f => f.name).join(', ')}{importFiles.length > 8 ? ` +${importFiles.length - 8} more` : ''}</Box>
          ) : null}
          {importStatus ? <Alert key="status" type="info">{importStatus}</Alert> : null}
          {importError ? <Alert key="error" type="error">{importError}</Alert> : null}
        </SpaceBetween>
      </Modal>

      {/* ── Stakeholder Dashboard ──────────────────────────────────── */}
      {metrics && (
        <SpaceBetween size="m">
          {/* KPI Cards */}
          <ColumnLayout columns={5} variant="text-grid">
            <Container>
              <Box variant="awsui-key-label">Total Servers</Box>
              <Box variant="awsui-value-large">{machines.length}</Box>
            </Container>
            <Container>
              <Box variant="awsui-key-label">Total CPU Cores</Box>
              <Box variant="awsui-value-large">{metrics.totalCPU.toLocaleString()}</Box>
            </Container>
            <Container>
              <Box variant="awsui-key-label">Total RAM</Box>
              <Box variant="awsui-value-large">{metrics.totalRAM.toLocaleString()} GB</Box>
            </Container>
            <Container>
              <Box variant="awsui-key-label">Total Storage</Box>
              <Box variant="awsui-value-large">{(metrics.totalStorage / 1024).toFixed(1)} TB</Box>
            </Container>
            <Container>
              <Box variant="awsui-key-label">Right-Sizing Opportunity</Box>
              <Box variant="awsui-value-large" color="text-status-warning">{metrics.underUtilized} servers</Box>
              <Box fontSize="body-s" color="text-body-secondary">{Math.round(metrics.underUtilized / machines.length * 100)}% under-utilized</Box>
            </Container>
          </ColumnLayout>

          {/* Distribution Row */}
          <ColumnLayout columns={3}>
            {/* OS Distribution - horizontal bars */}
            <Container header={<Header variant="h3">Operating Systems</Header>}>
              <SpaceBetween size="xxs">
                {Object.entries(metrics.osDist).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([os, count]) => (
                  <div key={os} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ width: 120, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{os}</div>
                    <div style={{ flex: 1, height: 16, backgroundColor: '#f2f3f3', borderRadius: 3, overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${(count / machines.length) * 100}%`, backgroundColor: os.includes('Windows') ? '#0972d3' : os.includes('Red Hat') ? '#d91515' : os.includes('Amazon') ? '#f89256' : '#2ea597', borderRadius: 3 }}></div>
                    </div>
                    <div style={{ width: 30, fontSize: 12, textAlign: 'right', fontWeight: 'bold' }}>{count}</div>
                  </div>
                ))}
              </SpaceBetween>
            </Container>

            {/* Utilization Breakdown - stacked bar */}
            <Container header={<Header variant="h3">CPU Utilization</Header>}>
              <SpaceBetween size="s">
                <div style={{ display: 'flex', height: 24, borderRadius: 4, overflow: 'hidden' }}>
                  {metrics.overUtilized > 0 && <div style={{ width: `${(metrics.overUtilized / machines.length) * 100}%`, backgroundColor: '#d91515' }} title={`Over: ${metrics.overUtilized}`}></div>}
                  {metrics.rightSized > 0 && <div style={{ width: `${(metrics.rightSized / machines.length) * 100}%`, backgroundColor: '#2ea597' }} title={`Right: ${metrics.rightSized}`}></div>}
                  {metrics.underUtilized > 0 && <div style={{ width: `${(metrics.underUtilized / machines.length) * 100}%`, backgroundColor: '#f89256' }} title={`Under: ${metrics.underUtilized}`}></div>}
                  {metrics.noData > 0 && <div style={{ width: `${(metrics.noData / machines.length) * 100}%`, backgroundColor: '#e9ebed' }} title={`No data: ${metrics.noData}`}></div>}
                </div>
                <ColumnLayout columns={2}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
                    <div style={{ width: 10, height: 10, borderRadius: 2, backgroundColor: '#d91515' }}></div>
                    Over-utilized (&gt;70%): <b>{metrics.overUtilized}</b>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
                    <div style={{ width: 10, height: 10, borderRadius: 2, backgroundColor: '#2ea597' }}></div>
                    Right-sized (20-70%): <b>{metrics.rightSized}</b>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
                    <div style={{ width: 10, height: 10, borderRadius: 2, backgroundColor: '#f89256' }}></div>
                    Under-utilized (&lt;20%): <b>{metrics.underUtilized}</b>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
                    <div style={{ width: 10, height: 10, borderRadius: 2, backgroundColor: '#e9ebed' }}></div>
                    No data: <b>{metrics.noData}</b>
                  </div>
                </ColumnLayout>
              </SpaceBetween>
            </Container>

            {/* Environment + Platform */}
            <Container header={<Header variant="h3">Environment & Platform</Header>}>
              <SpaceBetween size="s">
                <Box variant="awsui-key-label">Platform</Box>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {Object.entries(metrics.platDist).sort((a, b) => b[1] - a[1]).map(([p, count]) => (
                    <Badge key={p} color="blue">{p}: {count}</Badge>
                  ))}
                </div>
                <Box variant="awsui-key-label">Environment</Box>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {(() => {
                    const envDist: Record<string, number> = {};
                    machines.forEach(m => { const e = m.environment || 'unknown'; envDist[e] = (envDist[e] || 0) + 1; });
                    return Object.entries(envDist).sort((a, b) => b[1] - a[1]).map(([e, count]) => (
                      <Badge key={e} color={e === 'prod' ? 'red' : e === 'dev' ? 'grey' : 'blue'}>{e}: {count}</Badge>
                    ));
                  })()}
                </div>
              </SpaceBetween>
            </Container>
          </ColumnLayout>

          {/* EC2 Recommendations + Strategy */}
          {(Object.keys(metrics.ec2Dist).length > 0 || Object.keys(metrics.stratDist).length > 0) && (
            <ColumnLayout columns={2}>
              {Object.keys(metrics.ec2Dist).length > 0 && (
                <Container header={<Header variant="h3">EC2 Right-Sizing Recommendations</Header>}>
                  <SpaceBetween size="xxs">
                    {Object.entries(metrics.ec2Dist).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([type, count]) => (
                      <div key={type} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <div style={{ width: 120, fontSize: 12, fontWeight: 'bold' }}>{type}</div>
                        <div style={{ flex: 1, height: 16, backgroundColor: '#f2f3f3', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${(count / machines.length) * 100}%`, backgroundColor: '#0972d3', borderRadius: 3 }}></div>
                        </div>
                        <div style={{ width: 30, fontSize: 12, textAlign: 'right', fontWeight: 'bold' }}>{count}</div>
                      </div>
                    ))}
                  </SpaceBetween>
                </Container>
              )}
              {Object.keys(metrics.stratDist).length > 0 && (
                <Container header={<Header variant="h3">Suggested Migration Strategy</Header>}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    {Object.entries(metrics.stratDist).sort((a, b) => b[1] - a[1]).map(([s, count]) => (
                      <Badge key={s} color={s === 'rehost' ? 'grey' : s === 'replatform' ? 'blue' : 'green'}>
                        {s}: {count}
                      </Badge>
                    ))}
                  </div>
                </Container>
              )}
            </ColumnLayout>
          )}
        </SpaceBetween>
      )}

      {/* ── Detailed Table ─────────────────────────────────────────── */}
      <Table
        {...collectionProps}
        columnDefinitions={[
          { id: 'host', header: 'Hostname', cell: (i: any) => (
            <Button variant="link" onClick={() => setSelected(i)}>{i.hostname}</Button>
          ), sortingField: 'hostname', width: 180 },
          { id: 'ip', header: 'IP Address', cell: (i: any) => i.ip_address, width: 120 },
          { id: 'os', header: 'OS', cell: (i: any) => i.os_name || '—', width: 140 },
          { id: 'cpu', header: 'CPU', cell: (i: any) => `${i.cpu_cores || 0} cores`, width: 75 },
          { id: 'ram', header: 'RAM', cell: (i: any) => `${i.memory_gb || 0} GB`, width: 70 },
          { id: 'storage', header: 'Storage', cell: (i: any) => i.storage_gb ? `${i.storage_gb} GB` : '—', width: 80 },
          { id: 'cpu_util', header: 'CPU %', cell: (i: any) => {
            const v = i.avg_cpu_utilization || 0;
            const color = v > 70 ? 'red' : v < 20 && v > 0 ? 'grey' : 'blue';
            return v ? <Badge color={color}>{v}%</Badge> : '—';
          }, width: 70 },
          { id: 'ram_util', header: 'RAM %', cell: (i: any) => i.avg_memory_utilization ? <Badge>{i.avg_memory_utilization}%</Badge> : '—', width: 70 },
          { id: 'ec2', header: 'EC2 Recommendation', cell: (i: any) => i.ec2_recommendation ? <Badge color="blue">{i.ec2_recommendation}</Badge> : '—', width: 140 },
          { id: 'strategy', header: 'Strategy', cell: (i: any) => i.suggested_strategy ? <Badge color="green">{i.suggested_strategy}</Badge> : '—', width: 100 },
          { id: 'virt', header: 'Platform', cell: (i: any) => <Badge>{i.virtualization || 'unknown'}</Badge>, width: 85 },
        ]}
        items={items}
        loading={loading}
        trackBy="machine_id"
        variant="full-page"
        stickyHeader
        selectionType="multi"
        selectedItems={sel}
        onSelectionChange={({detail}) => setSel(detail.selectedItems)}
        header={
          <Header variant="h2" counter={`(${machines.length})`}
            actions={<SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={load} />
              <Button disabled={sel.length===0} onClick={async () => {
                try { const r = await apiClient.get('/portfolio/apps'); setApps(Array.isArray((r as any)?.data??r)?(r as any)?.data??r:[]); } catch { setApps([]); }
                setShowAssign(true);
              }}>Assign to Application ({sel.length})</Button>
            </SpaceBetween>}>
            Server Details
          </Header>
        }
        filter={<TextFilter {...filterProps} filteringPlaceholder="Search by hostname, IP, OS..." />}
        pagination={<Pagination {...paginationProps} />}
      />

      {/* ── Machine Detail Modal ───────────────────────────────────── */}
      {selected && (
        <Modal visible onDismiss={() => setSelected(null)} header={selected.hostname} size="large">
          <Tabs tabs={[
            { label: 'Overview', id: 'ov', content: (
              <Container>
                <ColumnLayout columns={3} variant="text-grid">
                  <div><Box variant="awsui-key-label">Hostname</Box><Box>{selected.hostname}</Box></div>
                  <div><Box variant="awsui-key-label">IP Address</Box><Box>{selected.ip_address}</Box></div>
                  <div><Box variant="awsui-key-label">DNS Name</Box><Box>{selected.dns_name || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">OS</Box><Box>{selected.os_name} {selected.os_version}</Box></div>
                  <div><Box variant="awsui-key-label">CPU</Box><Box>{selected.cpu_cores} cores ({selected.cpu_description || '—'})</Box></div>
                  <div><Box variant="awsui-key-label">RAM</Box><Box>{selected.memory_gb} GB</Box></div>
                  <div><Box variant="awsui-key-label">Storage</Box><Box>{selected.storage_gb || 0} GB (Free: {selected.storage_free_gb || '—'} GB)</Box></div>
                  <div><Box variant="awsui-key-label">Platform</Box><Box>{selected.virtualization}</Box></div>
                  <div><Box variant="awsui-key-label">Hypervisor Host</Box><Box>{selected.hypervisor_host || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Strategy</Box><Box>{selected.suggested_strategy ? <Badge color="green">{selected.suggested_strategy}</Badge> : '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Network/VLAN</Box><Box>{selected.network_name || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">MAC Address</Box><Box>{selected.mac_address || '—'}</Box></div>
                </ColumnLayout>
              </Container>
            )},
            { label: 'Performance', id: 'perf', content: (
              <Container>
                <ColumnLayout columns={3} variant="text-grid">
                  <div><Box variant="awsui-key-label">CPU Avg</Box><Box>{selected.avg_cpu_utilization || 0}%</Box></div>
                  <div><Box variant="awsui-key-label">CPU P95</Box><Box>{selected.cpu_p95 || 0}%</Box></div>
                  <div><Box variant="awsui-key-label">CPU Peak</Box><Box>{selected.peak_cpu_utilization || 0}%</Box></div>
                  <div><Box variant="awsui-key-label">RAM Avg</Box><Box>{selected.avg_memory_utilization || 0}%</Box></div>
                  <div><Box variant="awsui-key-label">RAM Peak</Box><Box>{selected.peak_memory_utilization || 0}%</Box></div>
                  <div><Box variant="awsui-key-label">RAM Active</Box><Box>{selected.ram_active || '—'}%</Box></div>
                  <div><Box variant="awsui-key-label">Disk IOPS Avg</Box><Box>{selected.disk_iops_avg || 0}</Box></div>
                  <div><Box variant="awsui-key-label">Read IOPS P95</Box><Box>{selected.storage_max_read_iops || 0}</Box></div>
                  <div><Box variant="awsui-key-label">Write IOPS P95</Box><Box>{selected.storage_max_write_iops || 0}</Box></div>
                  <div><Box variant="awsui-key-label">Net RX Max</Box><Box>{selected.network_rx_max_kbps ? `${selected.network_rx_max_kbps} KBps` : '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Net TX Max</Box><Box>{selected.network_tx_max_kbps ? `${selected.network_tx_max_kbps} KBps` : '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Disk Queue Max</Box><Box>{selected.queue_max || '—'}</Box></div>
                </ColumnLayout>
              </Container>
            )},
            { label: 'Right-Sizing', id: 'sizing', content: (
              <Container>
                <ColumnLayout columns={2} variant="text-grid">
                  <div><Box variant="awsui-key-label">EC2 Min (Shared)</Box><Box>{selected.ec2_min_shared || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">EC2 Max (Shared)</Box><Box>{selected.ec2_max_shared || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">EC2 Dedicated</Box><Box>{selected.ec2_dedicated || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">RDS Recommendation</Box><Box>{selected.rds_recommendation || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Suggested Strategy</Box><Box>{selected.suggested_strategy || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Core Licenses</Box><Box>{selected.core_licenses || '—'}</Box></div>
                </ColumnLayout>
                {(selected.ec2_recommendation) && (
                  <Box margin={{ top: 'm' }}>
                    <Alert type="info">
                      Recommended EC2: <strong>{selected.ec2_recommendation}</strong>
                      {selected.rds_recommendation && <> | RDS: <strong>{selected.rds_recommendation}</strong></>}
                    </Alert>
                  </Box>
                )}
              </Container>
            )},
            { label: 'Storage', id: 'disk', content: (
              <Container>
                <ColumnLayout columns={2} variant="text-grid">
                  <div><Box variant="awsui-key-label">Total Storage</Box><Box>{selected.storage_gb || 0} GB</Box></div>
                  <div><Box variant="awsui-key-label">Used Storage</Box><Box>{selected.storage_gb && selected.storage_free_gb ? (selected.storage_gb - selected.storage_free_gb) : '—'} GB</Box></div>
                  <div><Box variant="awsui-key-label">Free Storage</Box><Box>{selected.storage_free_gb || '—'} GB</Box></div>
                  <div><Box variant="awsui-key-label">Dense IO Storage</Box><Box>{selected.dense_io_gb || '—'} GB</Box></div>
                  <div><Box variant="awsui-key-label">Reads Avg (IOps)</Box><Box>{selected.reads_avg_iops || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Reads Max (IOps)</Box><Box>{selected.reads_max_iops || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Writes Avg (IOps)</Box><Box>{selected.writes_avg_iops || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Writes Max (IOps)</Box><Box>{selected.writes_max_iops || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Reads P95 (KBps)</Box><Box>{selected.reads_p95_kbps || '—'}</Box></div>
                  <div><Box variant="awsui-key-label">Writes P95 (KBps)</Box><Box>{selected.writes_p95_kbps || '—'}</Box></div>
                </ColumnLayout>
              </Container>
            )},
          ]} />
        </Modal>
      )}

      {/* ── Assign Modal ───────────────────────────────────────────── */}
      {showAssign && (
        <Modal visible onDismiss={() => setShowAssign(false)} header="Assign Machines to Application" size="medium"
          footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => setShowAssign(false)}>Cancel</Button>
            <Button variant="primary" disabled={!assignTarget} loading={assigning} onClick={async () => {
              if (!assignTarget) return;
              setAssigning(true);
              try {
                const appResp = await apiClient.get(`/portfolio/apps/${assignTarget.value}`);
                const appData = (appResp as any)?.data ?? appResp;
                const currentIds: string[] = appData.machine_ids || [];
                const newIds = [...new Set([...currentIds, ...sel.map((m:any) => m.machine_id)])];
                await apiClient.put(`/portfolio/apps/${assignTarget.value}`, {
                  ...appData, machine_ids: newIds, machine_count: newIds.length,
                });
                setSel([]);
                setShowAssign(false);
              } catch (e: any) {
                alert(`Assignment failed: ${e?.message || e}`);
              }
              setAssigning(false);
            }}>Assign {sel.length} Machine(s)</Button>
          </SpaceBetween></Box>}>
          <SpaceBetween size="m">
            <Box>Assigning <b>{sel.length}</b> machine(s): {sel.slice(0, 5).map((m:any) => m.hostname).join(', ')}{sel.length > 5 ? ` +${sel.length - 5} more` : ''}</Box>
            <FormField label="Target Application">
              <Select
                selectedOption={assignTarget}
                onChange={({detail}) => setAssignTarget(detail.selectedOption)}
                options={apps.map((a:any) => ({label:`${a.app_name} (${a.app_id})`, value:a.app_id}))}
                placeholder="Select application..."
                filteringType="auto"
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
};

export default DiscoveryPage;
