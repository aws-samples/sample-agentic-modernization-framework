// Deployed AgentCore resource names — backend /api/v1/infrastructure/.
import { apiClient } from './client';

export interface Infrastructure {
  region: string;
  project_prefix: string;
  environment: string;
  dynamodb_table: string;
  s3_bucket: string;
  graph_backend: string;
  wave_planning_enabled: boolean;
  harness: { id: string; arn: string; model: string };
  gateway: { id: string; endpoint: string };
  workflow: { state_machine: string; invoke_lambda: string };
  ecs: { cluster: string; task_definition: string; log_group: string };
}

export const getInfrastructure = (): Promise<Infrastructure> =>
  apiClient.get('/infrastructure/');
