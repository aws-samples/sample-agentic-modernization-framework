import React from 'react';
import { SideNavigation as CloudscapeSideNav } from '@cloudscape-design/components';
import { useNavigate, useLocation } from 'react-router-dom';
import { useCapabilities } from '../CapabilitiesContext';

export const SideNavigation: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const caps = useCapabilities();

  const items: any[] = [
    { type: 'link', text: 'Dashboard', href: '/' },
    { type: 'link', text: 'Program', href: '/program' },
    { type: 'divider' },
    { type: 'link', text: 'L1 Discovery', href: '/inventory' },
    { type: 'link', text: 'Workflow', href: '/workflow' },
    // Wave Planning needs a dependency source; graph_backend defaults to dynamodb.
    ...(caps.wave_planning_enabled ? [{ type: 'link', text: 'Wave Planning', href: '/wave-planning' }] : []),
    { type: 'link', text: 'Governance', href: '/governance' },
    { type: 'divider' },
    { type: 'link', text: 'Chat', href: '/chat' },
    { type: 'link', text: 'How It Works', href: '/how-we-do' },
    { type: 'link', text: 'Settings', href: '/settings' },
  ];

  return (
    <CloudscapeSideNav
      activeHref={location.pathname}
      onFollow={(e) => {
        e.preventDefault();
        navigate(e.detail.href);
      }}
      header={{ text: 'AMF', href: '/' }}
      items={items}
    />
  );
};
