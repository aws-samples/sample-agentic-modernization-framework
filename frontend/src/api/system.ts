// System capabilities (feature flags) — backend /api/v1/system/capabilities.
import { apiClient } from './client';

export interface Capabilities {
  environment: string;
  deployment_mode: string;
  graph_backend: string; // dynamodb | neptune | none
  wave_planning_enabled: boolean;
  deploy_enabled?: boolean;
  aws_region: string;
  agentcore_harness_id: string;
  bedrock_model_id: string;
  sfn_state_machine_name: string;
  // Regions offered as deployment targets (backend SUPPORTED_TARGET_REGIONS).
  supported_target_regions?: string[];
  // Poll cadences (ms) / cap — backend-owned tunables.
  validation_poll_ms?: number;
  validation_poll_max?: number;
  workflow_poll_ms?: number;
}

export const getCapabilities = (): Promise<Capabilities> =>
  apiClient.get('/system/capabilities');
