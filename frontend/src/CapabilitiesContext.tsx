// Capabilities context — fetches backend feature flags once and exposes them
// so the UI can adapt (e.g. hide Wave Planning when no graph backend is set).
import React, { createContext, useContext, useEffect, useState, ReactNode } from 'react';
import { getCapabilities, type Capabilities } from './api/system';

const DEFAULTS: Capabilities = {
  environment: 'dev',
  deployment_mode: 'agentcore',
  graph_backend: 'none',
  wave_planning_enabled: false,
  deploy_enabled: false,
  aws_region: '', // unknown until the fetch returns; console-link builders render nothing for ''
  agentcore_harness_id: '',
  bedrock_model_id: '',
  sfn_state_machine_name: '',
};

const CapabilitiesContext = createContext<Capabilities>(DEFAULTS);

export const CapabilitiesProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [caps, setCaps] = useState<Capabilities>(DEFAULTS);

  useEffect(() => {
    getCapabilities()
      .then((c) => c && setCaps({ ...DEFAULTS, ...c }))
      .catch(() => setCaps(DEFAULTS));
  }, []);

  return <CapabilitiesContext.Provider value={caps}>{children}</CapabilitiesContext.Provider>;
};

export const useCapabilities = () => useContext(CapabilitiesContext);
