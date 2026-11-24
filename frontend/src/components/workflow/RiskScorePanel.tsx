import { useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { computeRiskScore, getRiskScore } from '../../api/analysis';

type ScoreCategory = { category: string; score: number; max: number; reason: string };
type ScoreResult = {
  overall: number;
  max: number;
  tier: string;
  categories: ScoreCategory[];
  inputs_complete: boolean;
};

const tierColor = (tier: string): 'red' | 'blue' | 'green' | 'grey' => {
  const t = (tier || '').toLowerCase();
  if (t === 'high') return 'red';
  if (t === 'medium') return 'blue';
  if (t === 'low') return 'green';
  return 'grey';
};

interface Props {
  appId: string;
  /** Bumping this (e.g. after saving readiness answers) forces a fresh recompute. */
  refreshKey?: number;
}

/** Read-only migration risk score (Category / Score / Reason) with a Recompute action.
 *  Inputs are captured elsewhere (the app's Business/Team Readiness tabs). */
export default function RiskScorePanel({ appId, refreshKey }: Props) {
  const [score, setScore] = useState<ScoreResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Read the latest stored score on mount (no write on view).
  useEffect(() => {
    let cancelled = false;
    getRiskScore(appId)
      .then((records: any) => {
        const latest = Array.isArray(records) ? records[0] : null;
        if (!cancelled && latest?.result) setScore(latest.result as ScoreResult);
      })
      .catch(() => { /* no score yet */ });
    return () => { cancelled = true; };
  }, [appId]);

  // A refreshKey change means the readiness answers were just saved — recompute.
  useEffect(() => {
    if (refreshKey === undefined || refreshKey === 0) return;
    recompute();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  const recompute = async () => {
    setLoading(true);
    setError('');
    try {
      const r = (await computeRiskScore(appId)) as unknown as ScoreResult;
      setScore(r);
    } catch (e: any) {
      setError(e?.message || 'Failed to compute risk score');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h3"
          description="Deterministic migration risk score (0–100, higher = more risk), scored across Risk/EOSL, Complexity, Team Readiness and Business Readiness."
          actions={<Button loading={loading} onClick={recompute}>Recompute</Button>}
        >
          Migration Risk Score
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}

        {!score && !loading && (
          <StatusIndicator type="info">
            No risk score yet — answer the Business/Team Readiness tabs, save, then Recompute.
          </StatusIndicator>
        )}

        {score && (
          <>
            <ColumnLayout columns={2} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Overall Risk</Box>
                <Box fontSize="display-l" fontWeight="bold">
                  {score.overall}
                  <Box variant="span" color="text-body-secondary" fontSize="heading-m"> / {score.max}</Box>
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Risk Tier</Box>
                <Box fontSize="heading-l"><Badge color={tierColor(score.tier)}>{score.tier}</Badge></Box>
              </div>
            </ColumnLayout>

            <Table
              variant="embedded"
              columnDefinitions={[
                { id: 'category', header: 'Category', cell: (r: ScoreCategory) => <Box fontWeight="bold">{r.category}</Box>, width: 180 },
                { id: 'score', header: 'Score', cell: (r: ScoreCategory) => `${r.score} / ${r.max}`, width: 100 },
                { id: 'reason', header: 'Reason', cell: (r: ScoreCategory) => r.reason },
              ]}
              items={score.categories}
            />

            {!score.inputs_complete && (
              <Alert type="warning">
                Readiness questionnaire incomplete — unanswered items are scored as the highest risk.
              </Alert>
            )}
          </>
        )}
      </SpaceBetween>
    </Container>
  );
}
