// FailureReasonModal — why a run failed. The workflow ends every failure in a
// generic Fail state, so the backend reconstructs the step + reasons from the
// Step Functions history (GET /workflow/failure/{app_id}).
import React, { useEffect, useState } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Alert from '@cloudscape-design/components/alert';
import Spinner from '@cloudscape-design/components/spinner';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Button from '@cloudscape-design/components/button';
import { getFailureReason, type FailureReason } from '../../api/workflow';
import { useCapabilities } from '../../CapabilitiesContext';
import { consoleUrl } from '../../lib/aws';

const KIND_LABEL: Record<string, string> = {
  task_error: 'Step error',
  precondition: 'Inputs not ready (pre-check)',
  post_check: 'Output failed validation (post-check)',
  rejected: 'Rejected at approval gate',
  aborted: 'Cancelled',
  timeout: 'Timed out',
  unknown: 'Failed',
};

export const FailureReasonModal: React.FC<{ appId: string | null; onClose: () => void }> = ({ appId, onClose }) => {
  const { aws_region: region } = useCapabilities();
  const [data, setData] = useState<FailureReason | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!appId) return;
    let active = true;
    setData(null);
    setError('');
    getFailureReason(appId)
      .then((d) => { if (active) setData(d); })
      .catch((e) => { if (active) setError(e?.message || 'Could not load the failure reason.'); });
    return () => { active = false; };
  }, [appId]);

  const sfnUrl = data?.execution_arn
    ? consoleUrl(region, `/states/home?region=${region}#/v2/executions/details/${data.execution_arn}`)
    : '';

  return (
    <Modal
      visible={!!appId}
      onDismiss={onClose}
      header={`${appId} — why the run failed`}
      size="large"
      footer={<Box float="right"><Button variant="primary" onClick={onClose}>Close</Button></Box>}
    >
      {error && <Alert type="error">{error}</Alert>}
      {!error && !data && <Spinner size="large" />}
      {data && !data.failed && <Alert type="info">The latest run has not failed (status: {data.status}).</Alert>}
      {data && data.failed && (
        <SpaceBetween size="m">
          <Alert type="error" header={KIND_LABEL[data.kind] || 'Failed'}>{data.summary}</Alert>
          <ColumnLayout columns={3} variant="text-grid">
            <div><Box variant="awsui-key-label">Failed step</Box><Box>{data.failed_state || '—'}</Box></div>
            <div><Box variant="awsui-key-label">Phase</Box><Box>{data.phase || '—'}</Box></div>
            <div><Box variant="awsui-key-label">When</Box><Box>{data.at ? new Date(data.at).toLocaleString() : '—'}</Box></div>
          </ColumnLayout>
          {data.reasons.length > 0 && (
            <div>
              <Box variant="awsui-key-label">Reasons</Box>
              <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                {data.reasons.map((r, i) => <li key={i}><Box variant="span">{r}</Box></li>)}
              </ul>
            </div>
          )}
          {data.warnings.length > 0 && (
            <div>
              <Box variant="awsui-key-label">Warnings</Box>
              <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
                {data.warnings.map((w, i) => <li key={i}><Box variant="span" color="text-body-secondary">{w}</Box></li>)}
              </ul>
            </div>
          )}
          {(data.error || data.cause) && (
            <ExpandableSection headerText="Raw error">
              <Box variant="code"><pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{[data.error, data.cause].filter(Boolean).join('\n\n')}</pre></Box>
            </ExpandableSection>
          )}
          <Box fontSize="body-s" color="text-body-secondary">
            Fix the cause, then start the workflow again for this application.
            {sfnUrl && <> {' '}<a href={sfnUrl} target="_blank" rel="noopener">Open the run in Step Functions</a>.</>}
          </Box>
        </SpaceBetween>
      )}
    </Modal>
  );
};

export default FailureReasonModal;
