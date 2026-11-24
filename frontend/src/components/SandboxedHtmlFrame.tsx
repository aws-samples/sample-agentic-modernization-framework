import React from 'react';

/**
 * Renders untrusted (agent/repo-derived) HTML in a fully sandboxed iframe.
 *
 * A `srcdoc` iframe without `sandbox` is same-origin with the SPA, so a script in a report
 * could read the Cognito tokens in sessionStorage. `sandbox=""` (no allow-scripts, no
 * allow-same-origin) gives the frame an opaque origin and blocks scripts, forms, popups and
 * top navigation. Do not add sandbox tokens here.
 */
export const SandboxedHtmlFrame: React.FC<{ html: string; title?: string; height?: number }> = ({
  html, title = 'Report', height = 600,
}) => (
  <iframe
    srcDoc={html}
    sandbox=""
    referrerPolicy="no-referrer"
    style={{ width: '100%', height, border: 'none' }}
    title={title}
  />
);

export default SandboxedHtmlFrame;
