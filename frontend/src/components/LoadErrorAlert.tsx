import React from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';

interface LoadErrorAlertProps {
  message: string;
  onRetry: () => void;
  loading?: boolean;
  header?: string;
}

export const LoadErrorAlert: React.FC<LoadErrorAlertProps> = ({
  message,
  onRetry,
  loading = false,
  header = 'AMF data unavailable',
}) => (
  <Alert
    type="error"
    header={header}
    action={<Button iconName="refresh" onClick={onRetry} loading={loading}>Retry</Button>}
  >
    {message} Previously loaded data remains visible where available.
  </Alert>
);
