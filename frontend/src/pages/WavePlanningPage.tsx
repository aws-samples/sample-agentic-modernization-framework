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
import TextFilter from '@cloudscape-design/components/text-filter';
import Tabs from '@cloudscape-design/components/tabs';
import Alert from '@cloudscape-design/components/alert';
import Select from '@cloudscape-design/components/select';
import * as api from '../api/portfolio';
import { apiClient, apiErrorMessage } from '../api/client';
import { LoadErrorAlert } from '../components/LoadErrorAlert';
import { DEMO_READ_ONLY, DEMO_READ_ONLY_MESSAGE } from '../demo';
import { useRole } from '../auth/AuthContext';
import { Role } from '../auth/roles';
import { visibleWaves, canRemoveLastWave, waveLabel, withDependency, withoutDependency, isDeclared, parseWave, waveValue, UNASSIGNED } from '../workflow/waveBoard';

interface GraphNode {
  id: string; name: string; tier: string; strategy: string;
  business_unit: string; machines: number; outbound: number; inbound: number;
  x?: number; y?: number;
}
interface GraphEdge { source: string; target: string; weight: number; ports: string[]; types: string[]; }

// ── Force-directed graph layout ───────────────────────────────────────

function computeLayout(nodes: GraphNode[], edges: GraphEdge[], width: number, height: number): GraphNode[] {
  const positioned = nodes.map((n, i) => ({
    ...n,
    x: width / 2 + (Math.cos(2 * Math.PI * i / nodes.length)) * Math.min(width, height) * 0.35,
    y: height / 2 + (Math.sin(2 * Math.PI * i / nodes.length)) * Math.min(width, height) * 0.35,
  }));

  // Simple force simulation (50 iterations)
  for (let iter = 0; iter < 80; iter++) {
    // Repulsion between all nodes
    for (let i = 0; i < positioned.length; i++) {
      for (let j = i + 1; j < positioned.length; j++) {
        const dx = positioned[i].x! - positioned[j].x!;
        const dy = positioned[i].y! - positioned[j].y!;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = 8000 / (dist * dist);
        positioned[i].x! += (dx / dist) * force;
        positioned[i].y! += (dy / dist) * force;
        positioned[j].x! -= (dx / dist) * force;
        positioned[j].y! -= (dy / dist) * force;
      }
    }
    // Attraction along edges
    for (const edge of edges) {
      const s = positioned.find(n => n.id === edge.source);
      const t = positioned.find(n => n.id === edge.target);
      if (!s || !t) continue;
      const dx = t.x! - s.x!;
      const dy = t.y! - s.y!;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - 150) * 0.01;
      s.x! += (dx / dist) * force;
      s.y! += (dy / dist) * force;
      t.x! -= (dx / dist) * force;
      t.y! -= (dy / dist) * force;
    }
    // Center gravity
    for (const n of positioned) {
      n.x! += (width / 2 - n.x!) * 0.01;
      n.y! += (height / 2 - n.y!) * 0.01;
      // Bounds
      n.x! = Math.max(60, Math.min(width - 60, n.x!));
      n.y! = Math.max(40, Math.min(height - 40, n.y!));
    }
  }
  return positioned;
}

// ── Dependency Graph Component ────────────────────────────────────────

const DependencyGraph: React.FC<{ nodes: GraphNode[]; edges: GraphEdge[]; onNodeClick: (id: string) => void; selectedNode: string | null; filter: string }> = ({ nodes, edges, onNodeClick, selectedNode, filter }) => {
  const width = 1400, height = 800;
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  // Only show connected nodes + selected node neighbors (skip isolated nodes for clarity)
  const connectedIds = new Set<string>();
  edges.forEach(e => { connectedIds.add(e.source); connectedIds.add(e.target); });
  
  const filteredNodes = nodes.filter(n => {
    if (!filter) return true; // Show ALL nodes (including isolated)
    const s = filter.toLowerCase();
    return n.name.toLowerCase().includes(s) || n.id.toLowerCase().includes(s) || n.business_unit.toLowerCase().includes(s);
  });
  const filteredIds = new Set(filteredNodes.map(n => n.id));
  const filteredEdges = edges.filter(e => filteredIds.has(e.source) && filteredIds.has(e.target));

  const positioned = computeLayout(filteredNodes, filteredEdges, width, height);

  const getNodeColor = (n: GraphNode) => {
    if (n.id === selectedNode) return '#0972d3';
    if (n.tier === 'tier_1') return '#d91515';
    if (n.strategy === 'refactor') return '#037f0c';
    if (n.strategy === 'replatform') return '#0972d3';
    return '#5f6b7a';
  };

  const getNodeRadius = (n: GraphNode) => Math.max(16, Math.min(35, 16 + (n.outbound + n.inbound) / 3));

  const handleMouseDown = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).tagName === 'circle' || (e.target as HTMLElement).tagName === 'text') return;
    setDragging(true);
    setDragStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
  };
  const handleMouseMove = (e: React.MouseEvent) => {
    if (!dragging) return;
    setPan({ x: e.clientX - dragStart.x, y: e.clientY - dragStart.y });
  };
  const handleMouseUp = () => setDragging(false);
  const handleWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    setZoom(z => Math.max(0.3, Math.min(3, z * delta)));
  };

  const svgHeight = 550;

  return (
    <div style={{ overflow: 'hidden', border: '1px solid #e9ebed', borderRadius: 8, background: '#ffffff', position: 'relative', height: svgHeight, cursor: dragging ? 'grabbing' : 'grab' }}>
      <svg
        width="100%" height={svgHeight}
        onMouseDown={handleMouseDown} onMouseMove={handleMouseMove} onMouseUp={handleMouseUp} onMouseLeave={handleMouseUp}
        onWheel={handleWheel}
      >
        <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
          {/* Arrow marker */}
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#6b7280" />
            </marker>
          </defs>
          {/* Edges */}
          {filteredEdges.map((e, i) => {
            const s = positioned.find(n => n.id === e.source);
            const t = positioned.find(n => n.id === e.target);
            if (!s || !t) return null;
            const isHighlighted = selectedNode === e.source || selectedNode === e.target;
            return (
              <g key={`edge-${i}`}>
                <line
                  x1={s.x} y1={s.y} x2={t.x} y2={t.y}
                  stroke={isHighlighted ? '#0972d3' : '#6b7280'}
                  strokeWidth={isHighlighted ? 3 : Math.max(1.5, Math.min(4, e.weight / 2))}
                  opacity={selectedNode && !isHighlighted ? 0.15 : 0.6}
                  strokeDasharray={isDeclared(e.types) ? '6 4' : undefined}
                  markerEnd="url(#arrow)"
                />
            {isHighlighted && (
              <text x={(s.x! + t.x!) / 2} y={(s.y! + t.y!) / 2 - 6} fontSize={10} fill="#0972d3" textAnchor="middle">
                {isDeclared(e.types) && e.ports.length === 0 ? 'declared' : `${e.weight} conn`} {e.ports.length > 0 ? `(${e.ports.slice(0, 2).join(',')})` : ''}
              </text>
            )}
          </g>
        );
      })}
      {/* Nodes */}
      {positioned.map(n => {
        const r = getNodeRadius(n);
        const dimmed = selectedNode && selectedNode !== n.id && !filteredEdges.some(e => (e.source === selectedNode && e.target === n.id) || (e.target === selectedNode && e.source === n.id));
        return (
          <g key={n.id} onClick={() => onNodeClick(n.id)} style={{ cursor: 'pointer' }} opacity={dimmed ? 0.3 : 1}>
            <circle cx={n.x} cy={n.y} r={r} fill={getNodeColor(n)} stroke={n.id === selectedNode ? '#000' : '#fff'} strokeWidth={n.id === selectedNode ? 3 : 2} />
            <text x={n.x} y={n.y! + r + 14} fontSize={11} textAnchor="middle" fill="#16191f" fontWeight={n.id === selectedNode ? 'bold' : 'normal'}>
              {n.name.length > 20 ? n.name.slice(0, 18) + '…' : n.name}
            </text>
            <text x={n.x} y={n.y! + 4} fontSize={10} textAnchor="middle" fill="#fff" fontWeight="bold">
              {n.machines}
            </text>
          </g>
        );
      })}
        </g>
      </svg>
      {/* Zoom controls */}
      <div style={{ position: 'absolute', top: 8, right: 8, display: 'flex', gap: 4 }}>
        <button onClick={() => setZoom(z => Math.min(3, z * 1.2))} style={{ width: 28, height: 28, cursor: 'pointer', borderRadius: 4, border: '1px solid #ccc', background: '#fff', fontSize: 16 }}>+</button>
        <button onClick={() => setZoom(z => Math.max(0.3, z * 0.8))} style={{ width: 28, height: 28, cursor: 'pointer', borderRadius: 4, border: '1px solid #ccc', background: '#fff', fontSize: 16 }}>−</button>
        <button onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }} style={{ width: 28, height: 28, cursor: 'pointer', borderRadius: 4, border: '1px solid #ccc', background: '#fff', fontSize: 11 }}>⟲</button>
      </div>
    </div>
  );
};

// ── Wave Insights Component ────────────────────────────────────────────

const WaveInsights: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiClient.get('/graph/wave-insights').then((r: any) => {
      setData(r?.data ?? r);
    }).catch(() => setData(null)).finally(() => setLoading(false));
  }, []);

  if (loading) return <StatusIndicator type="loading">Loading wave insights...</StatusIndicator>;
  if (!data) return <Box color="text-body-secondary">No dependency insights yet. Upload an L1 discovery export so connections can be derived.</Box>;

  return (
    <SpaceBetween size="l">
      {/* Shared Databases */}
      {data.shared_databases?.length > 0 && (
        <Container header={<Header variant="h3" description="Applications connected via database ports — must migrate together or DB last">Shared Database Connections</Header>}>
          <Table
            columnDefinitions={[
              { id: 'source', header: 'Source App', cell: (r: any) => <Badge>{r.source_name}</Badge>, width: 180 },
              { id: 'target', header: 'Target App (DB)', cell: (r: any) => <Badge color="red">{r.target_name}</Badge>, width: 180 },
              { id: 'ports', header: 'DB Ports', cell: (r: any) => r.db_ports?.join(', '), width: 100 },
              { id: 'conns', header: 'Connections', cell: (r: any) => r.connections, width: 90 },
              { id: 'constraint', header: 'Wave Constraint', cell: (r: any) => <Box color="text-status-warning">{r.constraint}</Box>, width: 250 },
            ]}
            items={data.shared_databases}
            variant="embedded"
          />
        </Container>
      )}

      {/* Complexity Scores */}
      <Container header={<Header variant="h3" description="Higher score = more complex migration. Pilot with low-complexity apps first.">Migration Complexity</Header>}>
        <Table
          columnDefinitions={[
            { id: 'name', header: 'Application', cell: (r: any) => <Box fontWeight="bold">{r.app_name}</Box>, width: 180 },
            { id: 'tier', header: 'Tier', cell: (r: any) => <Badge color={r.tier === 'tier_1' ? 'red' : 'blue'}>{r.tier}</Badge>, width: 70 },
            { id: 'machines', header: 'Machines', cell: (r: any) => r.machines, width: 70 },
            { id: 'deps', header: 'Dependencies', cell: (r: any) => `↑${r.outbound} ↓${r.inbound}`, width: 90 },
            { id: 'score', header: 'Complexity', cell: (r: any) => <Badge color={r.complexity_score > 8 ? 'red' : r.complexity_score > 3 ? 'blue' : 'green'}>{r.complexity_score}</Badge>, width: 90 },
            { id: 'wave', header: 'Recommended', cell: (r: any) => <Badge color={r.wave_recommendation === 'pilot' ? 'green' : r.wave_recommendation === 'early' ? 'blue' : 'grey'}>{r.wave_recommendation}</Badge>, width: 100 },
            { id: 'strategy', header: 'Strategy', cell: (r: any) => r.strategy || '—', width: 90 },
          ]}
          items={(data.complexity_scores || []).slice(0, 25)}
          variant="embedded"
        />
      </Container>

      {/* Recommended Wave Order */}
      {data.recommended_order?.length > 0 && (
        <Container header={<Header variant="h3">Recommended Wave Order</Header>}>
          <ColumnLayout columns={Math.min(data.recommended_order.length, 5)}>
            {data.recommended_order.map((wave: any) => (
              <div key={wave.wave}>
                <Box variant="awsui-key-label">Wave {wave.wave}</Box>
                {wave.apps.map((appId: string) => {
                  const app = data.complexity_scores?.find((a: any) => a.app_id === appId);
                  return <Box key={appId} fontSize="body-s">{app?.app_name || appId}</Box>;
                })}
              </div>
            ))}
          </ColumnLayout>
        </Container>
      )}
    </SpaceBetween>
  );
};

// ── Main Page ─────────────────────────────────────────────────────────

// ── Wave Assignments (drag-and-drop Kanban between waves) ────────────

const LatestPlan: React.FC<{ plan: any }> = ({ plan }) => {
  if (!plan || !Array.isArray(plan.waves)) {
    return <Box textAlign="center" padding="l" color="text-body-secondary">No wave plan yet. Click Generate Wave Plan.</Box>;
  }
  const notReady: any[] = Array.isArray(plan.not_ready) ? plan.not_ready : [];
  const conflicts: any[] = Array.isArray(plan.conflicts) ? plan.conflicts : [];
  return (
    <SpaceBetween size="m">
      <Box fontSize="body-s" color="text-body-secondary">
        Generated {plan.generated_at ? new Date(plan.generated_at).toLocaleString() : '—'}{plan.planner ? ` · planner ${plan.planner}` : ' · legacy planner (regenerate to apply the playbook)'}
      </Box>
      {conflicts.length > 0 && (
        <Alert type="warning" header={`${conflicts.length} rule conflict(s)`}>
          <ul style={{ margin: 0, paddingLeft: 18 }}>{conflicts.map((c, i) => <li key={i}>{c.message || JSON.stringify(c)}</li>)}</ul>
        </Alert>
      )}
      {plan.waves.map((w: any) => (
        <Container key={w.wave_number} header={<Header variant="h3" counter={`(${(w.apps || []).length})`} description={w.criteria}>{`Wave ${w.wave_number} · ${w.label || w.band || ''}`}</Header>}>
          <Table
            variant="embedded"
            items={w.apps || []}
            columnDefinitions={[
              { id: 'app', header: 'Application', cell: (a: any) => <span><b>{a.app_name || a.app_id}</b> <Box variant="small" display="inline">{a.app_id}</Box></span>, width: 260 },
              { id: 'strategy', header: 'Strategy', cell: (a: any) => a.strategy ? <Badge>{a.strategy}</Badge> : '—', width: 110 },
              { id: 'tier', header: 'Tier', cell: (a: any) => <Badge color={a.tier === 'tier_1' ? 'red' : 'blue'}>{a.tier || '—'}</Badge>, width: 80 },
              { id: 'scores', header: 'Risk / Cx / Prio', cell: (a: any) => a.risk !== undefined ? `${a.risk} / ${a.complexity} / P${a.priority}` : '—', width: 130 },
              { id: 'deps', header: 'Depends on', cell: (a: any) => (a.depends_on || []).join(', ') || '—', width: 180 },
              { id: 'reason', header: 'Why here', cell: (a: any) => a.reason || '—' },
            ]}
          />
        </Container>
      ))}
      {notReady.length > 0 && (
        <Container header={<Header variant="h3" counter={`(${notReady.length})`} description="Not planned until Discovery/Assessment records a strategy">Not ready</Header>}>
          <SpaceBetween direction="horizontal" size="xs">
            {notReady.map((a: any) => <Badge key={a.app_id} color="grey">{a.app_name || a.app_id}</Badge>)}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
};

const WaveAssignments: React.FC<{ apps: any[]; onSave: (updates: {app_id: string; wave_number: number | string}[]) => Promise<void> }> = ({ apps, onSave }) => {
  const [assignments, setAssignments] = useState<Record<string, number>>({});
  const [saving, setSaving] = useState(false);
  const [dragItem, setDragItem] = useState<string | null>(null);
  const [shownMax, setShownMax] = useState(0);
  const { hasRole } = useRole();
  const canEdit = hasRole(Role.ENGINEER);

  useEffect(() => {
    const initial: Record<string, number> = {};
    apps.forEach(a => { initial[a.app_id] = parseWave(a.wave_number); });
    setAssignments(initial);
  }, [apps]);

  const waves = visibleWaves(assignments, shownMax);
  const lastWave = waves[waves.length - 1];
  const waveOptions = waves.map(w => ({ label: waveLabel(w), value: String(w) }));
  const moveTo = (appId: string, wave: number) => setAssignments(prev => ({ ...prev, [appId]: wave }));

  const getAppsInWave = (waveNum: number) => apps.filter(a => (assignments[a.app_id] ?? UNASSIGNED) === waveNum);

  const handleDragStart = (e: React.DragEvent, appId: string) => {
    setDragItem(appId);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', appId);
  };

  const handleDrop = (e: React.DragEvent, targetWave: number) => {
    e.preventDefault();
    const appId = e.dataTransfer.getData('text/plain') || dragItem;
    if (appId) {
      setAssignments(prev => ({ ...prev, [appId]: targetWave }));
    }
    setDragItem(null);
  };

  const handleDragOver = (e: React.DragEvent) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; };

  const handleSave = async () => {
    setSaving(true);
    const updates = Object.entries(assignments)
      .filter(([appId, wave]) => {
        const app = apps.find(a => a.app_id === appId);
        return app && parseWave(app.wave_number) !== wave;
      })
      .map(([app_id, wave]) => ({ app_id, wave_number: waveValue(wave) }));
    await onSave(updates);
    setSaving(false);
  };

  const changeCount = Object.entries(assignments).filter(([appId, wave]) => {
    const app = apps.find(a => a.app_id === appId);
    return app && parseWave(app.wave_number) !== wave;
  }).length;

  return (
    <SpaceBetween size="m">
      <SpaceBetween direction="horizontal" size="xs">
        <Button iconName="add-plus" onClick={() => setShownMax(lastWave + 1)} disabled={!canEdit}>Add wave</Button>
        <Button onClick={() => setShownMax(lastWave - 1)} disabled={!canEdit || !canRemoveLastWave(assignments, shownMax)}>
          Remove empty {waveLabel(lastWave)}
        </Button>
        <Button variant="primary" onClick={handleSave} loading={saving} disabled={!canEdit || changeCount === 0}>
          Save Changes ({changeCount})
        </Button>
      </SpaceBetween>
      <Box fontSize="body-s" color="text-body-secondary">Waves follow the planning playbook (0 Pilot · 1 Foundation · 2 Core · 3 Customer-Facing · then spill-over / Long Tail). Drag applications between waves, or use "Move to" on a card. Add as many waves as you need. Changes are saved when you click Save.</Box>
      <div style={{ display: 'flex', gap: 16, overflowX: 'auto', paddingBottom: 8 }}>
        {waves.map(waveNum => {
          const waveApps = getAppsInWave(waveNum);
          return (
            <div
              key={waveNum}
              onDragOver={handleDragOver}
              onDrop={(e) => handleDrop(e, waveNum)}
              style={{
                minWidth: 220, maxWidth: 280, flex: '0 0 auto',
                background: dragItem ? '#f0f4ff' : '#fafafa',
                border: '2px dashed #d5dbdb', borderRadius: 8,
                padding: 12, minHeight: 300,
              }}
            >
              <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 8, borderBottom: '2px solid #0972d3', paddingBottom: 4 }}>
                {waveNum === UNASSIGNED ? '⏳ Unassigned' : `🌊 ${waveLabel(waveNum)}`}
                <span style={{ float: 'right', fontWeight: 400, color: '#687078' }}>{waveApps.length}</span>
              </div>
              {waveApps.map(a => (
                <div
                  key={a.app_id}
                  draggable
                  onDragStart={(e) => handleDragStart(e, a.app_id)}
                  style={{
                    background: '#fff', border: '1px solid #d5dbdb', borderRadius: 6,
                    padding: '8px 10px', marginBottom: 6, cursor: 'grab',
                    boxShadow: dragItem === a.app_id ? '0 2px 8px rgba(0,0,0,0.15)' : 'none',
                    opacity: dragItem === a.app_id ? 0.5 : 1,
                    fontSize: 13,
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{a.app_name}</div>
                  <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
                    <Badge color={a.tier === 'tier_1' ? 'red' : 'blue'}>{a.tier}</Badge>
                    {a.migration_strategy && <Badge>{a.migration_strategy}</Badge>}
                  </div>
                  <div style={{ marginTop: 6 }}>
                    <Select
                      expandToViewport
                      disabled={!canEdit}
                      ariaLabel={`Move ${a.app_name} to wave`}
                      selectedOption={{ label: `Move to: ${waveLabel(assignments[a.app_id] ?? UNASSIGNED)}`, value: String(assignments[a.app_id] ?? UNASSIGNED) }}
                      options={waveOptions}
                      onChange={({ detail }) => moveTo(a.app_id, parseInt(detail.selectedOption.value ?? String(UNASSIGNED), 10))}
                    />
                  </div>
                </div>
              ))}
              {waveApps.length === 0 && (
                <div style={{ color: '#9ba7b6', fontSize: 12, textAlign: 'center', padding: 20 }}>
                  Drop apps here
                </div>
              )}
            </div>
          );
        })}
      </div>
    </SpaceBetween>
  );
};

export const WavePlanningPage: React.FC = () => {
  const [apps, setApps] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [graphLoading, setGraphLoading] = useState(true);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const [selectedApps, setSelectedApps] = useState<any[]>([]);
  const [running, setRunning] = useState(false);
  const [alert, setAlert] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadApps = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.listApps();
      setApps(Array.isArray((r as any)?.data ?? r) ? (r as any)?.data ?? r : []);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
    setLoading(false);
  }, []);

  const [plan, setPlan] = useState<any>(null);
  const loadPlan = useCallback(async () => {
    try {
      const r: any = await apiClient.get('/waves/plan');
      setPlan((r?.data ?? r) || null);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
  }, []);

  const loadGraph = useCallback(async () => {
    setGraphLoading(true);
    try {
      const r: any = await apiClient.get('/graph/topology');
      const data = r?.data ?? r;
      setNodes(data?.nodes || []);
      setEdges(data?.edges || []);
    } catch (error) {
      setLoadError(apiErrorMessage(error));
    }
    setGraphLoading(false);
  }, []);

  useEffect(() => {
    setLoadError(null);
    loadApps();
    loadGraph();
    loadPlan();
  }, [loadApps, loadGraph, loadPlan]);

  const selectedNodeData = nodes.find(n => n.id === selectedNode);
  const { hasRole } = useRole();
  const canEditDeps = hasRole(Role.ENGINEER);
  const [depTarget, setDepTarget] = useState<string>('');
  const [depSaving, setDepSaving] = useState(false);
  const selectedApp = apps.find(a => a.app_id === selectedNode);
  const declaredDeps: string[] = (selectedApp?.depends_on as string[]) || [];
  const saveDeps = async (next: string[]) => {
    if (!selectedNode) return;
    setDepSaving(true);
    try {
      await apiClient.put(`/portfolio/apps/${selectedNode}`, { depends_on: next });
      setAlert({ type: 'success', message: `Dependencies of ${selectedNode} updated` });
      setDepTarget('');
      await Promise.all([loadApps(), loadGraph()]);
    } catch (error) {
      setAlert({ type: 'error', message: apiErrorMessage(error) });
    }
    setDepSaving(false);
  };
  const outboundEdges = edges.filter(e => e.source === selectedNode);
  const inboundEdges = edges.filter(e => e.target === selectedNode);

  // Top hubs
  const hubs = [...nodes].sort((a, b) => (b.outbound + b.inbound) - (a.outbound + a.inbound)).slice(0, 15);

  return (
    <SpaceBetween size="l">
      <Header variant="h1" description="Application dependency graph derived from L1 discovery connections"
        actions={
          <SpaceBetween size="xs" direction="horizontal">
            <Button iconName="refresh" onClick={() => { setLoadError(null); loadApps(); loadGraph(); }} />
            <Button variant="primary" onClick={async () => {
              setRunning(true);
              try {
                const r: any = await apiClient.post('/workflow/wave-plan/agent');
                const d = r?.data ?? r;
                const s = d?.summary || {};
                setAlert({ type: (s.conflicts || 0) > 0 ? 'info' : 'success',
                  message: `Wave plan generated: ${s.total_apps ?? 0} apps in ${s.total_waves ?? 0} waves, ${s.not_ready ?? 0} not ready, ${s.conflicts ?? 0} conflict(s). See the Latest Plan tab.` });
                await Promise.all([loadApps(), loadGraph(), loadPlan()]);
              } catch (e: any) { setAlert({ type: 'error', message: e?.message || 'Failed' }); }
              setRunning(false);
            }} loading={running} disabled={DEMO_READ_ONLY}>
              Generate Wave Plan
            </Button>
          </SpaceBetween>
        }
      >
        Wave Planning
      </Header>

      {DEMO_READ_ONLY && <Alert type="info" header="Read-only demo mode">{DEMO_READ_ONLY_MESSAGE}</Alert>}
      {loadError && <LoadErrorAlert message={loadError} onRetry={() => { setLoadError(null); loadApps(); loadGraph(); }} loading={loading || graphLoading} />}
      {alert && <Alert type={alert.type} dismissible onDismiss={() => setAlert(null)}>{alert.message}</Alert>}

      <Tabs tabs={[
        { id: 'apps', label: 'Applications', content: (
          <Table
            columnDefinitions={[
              { id: 'name', header: 'Application', cell: (a: any) => <Box fontWeight="bold">{a.app_name}</Box>, width: 180 },
              { id: 'tier', header: 'Tier', cell: (a: any) => <Badge color={a.tier === 'tier_1' ? 'red' : 'blue'}>{a.tier?.replace('_', ' ')?.toUpperCase() || '—'}</Badge>, width: 80 },
              { id: 'strategy', header: 'Strategy', cell: (a: any) => a.migration_strategy ? <Badge color="blue">{a.migration_strategy}</Badge> : '—', width: 100 },
              { id: 'machines', header: 'Machines', cell: (a: any) => a.machine_count || 0, width: 80 },
              { id: 'wave', header: 'Wave', cell: (a: any) => parseWave(a.wave_number) !== UNASSIGNED ? <Badge color="green">W{a.wave_number}</Badge> : '—', width: 70 },
              { id: 'bu', header: 'BU', cell: (a: any) => a.business_unit || '—', width: 100 },
            ]}
            items={apps}
            loading={loading}
            trackBy="app_id"
            selectionType="multi"
            selectedItems={selectedApps}
            onSelectionChange={({ detail }) => setSelectedApps(detail.selectedItems)}
            header={<Header counter={`(${apps.length})`} description="Select applications to include in wave plan generation">Applications</Header>}
            empty={<Box textAlign="center">No applications</Box>}
          />
        )},
        ...[
          { id: 'graph', label: 'Dependency Graph', content: (
            <SpaceBetween size="m">
              <TextFilter filteringText={filterText} onChange={({ detail }) => setFilterText(detail.filteringText)} filteringPlaceholder="Search apps by name, BU..." />
              {graphLoading ? <StatusIndicator type="loading">Loading graph...</StatusIndicator> : nodes.length === 0 ? (
                <Box textAlign="center" padding="l" color="text-body-secondary">No dependency graph yet. Upload an L1 discovery export to populate connections.</Box>
              ) : (
                <ColumnLayout columns={1}>
                  <div>
                    <DependencyGraph nodes={nodes} edges={edges} onNodeClick={(id) => setSelectedNode(id === selectedNode ? null : id)} selectedNode={selectedNode} filter={filterText} />
                    <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xs' }}>
                      Click a node to see details and link it to other apps. Node size = connection volume. Red = Tier 1. Dashed = declared dependency.
                    </Box>
                  </div>
                  {selectedNode && selectedNodeData && (
                    <Container header={<Header variant="h3">{selectedNodeData.name}</Header>}>
                      <SpaceBetween size="s">
                        <ColumnLayout columns={2} variant="text-grid">
                          <div><Box variant="awsui-key-label">App ID</Box><Box>{selectedNodeData.id}</Box></div>
                          <div><Box variant="awsui-key-label">Tier</Box><Badge color={selectedNodeData.tier === 'tier_1' ? 'red' : 'blue'}>{selectedNodeData.tier}</Badge></div>
                          <div><Box variant="awsui-key-label">Strategy</Box><Badge>{selectedNodeData.strategy || '—'}</Badge></div>
                          <div><Box variant="awsui-key-label">Machines</Box><Box>{selectedNodeData.machines}</Box></div>
                          <div><Box variant="awsui-key-label">Outbound</Box><Box>{selectedNodeData.outbound} connections</Box></div>
                          <div><Box variant="awsui-key-label">Inbound</Box><Box>{selectedNodeData.inbound} connections</Box></div>
                        </ColumnLayout>
                        <Box variant="awsui-key-label">Declared dependencies (this app needs)</Box>
                        <SpaceBetween direction="horizontal" size="xs">
                          {declaredDeps.length === 0 && <Box color="text-body-secondary" fontSize="body-s">None declared</Box>}
                          {declaredDeps.map(d => (
                            <SpaceBetween key={d} direction="horizontal" size="xxs">
                              <Badge color="grey">{apps.find(a => a.app_id === d)?.app_name || d}</Badge>
                              <Button variant="inline-icon" iconName="close" ariaLabel={`Remove dependency on ${d}`} disabled={!canEditDeps || depSaving} onClick={() => saveDeps(withoutDependency(declaredDeps, d))} />
                            </SpaceBetween>
                          ))}
                        </SpaceBetween>
                        <SpaceBetween direction="horizontal" size="xs">
                          <Select
                            expandToViewport
                            filteringType="auto"
                            placeholder="Link to app…"
                            disabled={!canEditDeps}
                            selectedOption={depTarget ? { label: apps.find(a => a.app_id === depTarget)?.app_name || depTarget, value: depTarget } : null}
                            options={apps.filter(a => a.app_id !== selectedNode && !declaredDeps.includes(a.app_id)).map(a => ({ label: a.app_name || a.app_id, value: a.app_id, description: a.app_id }))}
                            onChange={({ detail }) => setDepTarget(detail.selectedOption.value || '')}
                          />
                          <Button iconName="add-plus" disabled={!canEditDeps || !depTarget} loading={depSaving} onClick={() => saveDeps(withDependency(declaredDeps, selectedNode as string, depTarget))}>
                            Add dependency
                          </Button>
                        </SpaceBetween>
                        {outboundEdges.length > 0 && (
                          <>
                            <Box variant="awsui-key-label">Outbound Dependencies ({outboundEdges.length})</Box>
                            {outboundEdges.map(e => (
                              <div key={e.target} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }} onClick={() => setSelectedNode(e.target)}>
                                <span>→</span>
                                <Badge color="blue">{nodes.find(n => n.id === e.target)?.name || e.target}</Badge>
                                <span>{isDeclared(e.types) && e.ports.length === 0 ? 'declared' : `${e.weight} conn`}</span>
                              </div>
                            ))}
                          </>
                        )}
                        {inboundEdges.length > 0 && (
                          <>
                            <Box variant="awsui-key-label">Inbound Dependencies ({inboundEdges.length})</Box>
                            {inboundEdges.map(e => (
                              <div key={e.source} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }} onClick={() => setSelectedNode(e.source)}>
                                <span>←</span>
                                <Badge color="green">{nodes.find(n => n.id === e.source)?.name || e.source}</Badge>
                                <span>{isDeclared(e.types) && e.ports.length === 0 ? 'declared' : `${e.weight} conn`}</span>
                              </div>
                            ))}
                          </>
                        )}
                      </SpaceBetween>
                    </Container>
                  )}
                </ColumnLayout>
              )}
            </SpaceBetween>
          )},
          { id: 'hubs', label: 'Dependency Hubs', content: (
            <Table
              columnDefinitions={[
                { id: 'name', header: 'Application', cell: (n: any) => <Button variant="link" onClick={() => { setSelectedNode(n.id); }}>{n.name}</Button>, width: 200 },
                { id: 'tier', header: 'Tier', cell: (n: any) => <Badge color={n.tier === 'tier_1' ? 'red' : 'blue'}>{n.tier}</Badge>, width: 80 },
                { id: 'outbound', header: 'Outbound', cell: (n: any) => n.outbound, width: 80 },
                { id: 'inbound', header: 'Inbound', cell: (n: any) => n.inbound, width: 80 },
                { id: 'total', header: 'Total', cell: (n: any) => <Badge color="blue">{n.outbound + n.inbound}</Badge>, width: 80 },
                { id: 'strategy', header: 'Strategy', cell: (n: any) => n.strategy ? <Badge>{n.strategy}</Badge> : '—', width: 100 },
              ]}
              items={hubs}
              header={<Header variant="h3" description="Most connected — migrate carefully">Dependency Hubs</Header>}
              variant="embedded"
            />
          )},
          { id: 'plan', label: 'Latest Plan', content: <LatestPlan plan={plan} /> },
          { id: 'insights', label: 'Wave Insights', content: <WaveInsights /> },
          { id: 'assignments', label: 'Wave Assignments', content: (
            <WaveAssignments apps={apps} onSave={async (updates: {app_id: string; wave_number: number | string}[]) => {
              try {
                for (const u of updates) {
                  await apiClient.put(`/portfolio/apps/${u.app_id}`, { wave_number: u.wave_number });
                }
                setAlert({ type: 'success', message: `Updated ${updates.length} wave assignment(s)` });
                await loadApps();
              } catch (e: any) { setAlert({ type: 'error', message: e?.message || 'Save failed' }); }
            }} />
          )},
        ],
      ]} />
    </SpaceBetween>
  );
};

export default WavePlanningPage;
