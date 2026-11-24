import React from 'react';
import { TopNavigation as CloudscapeTopNav } from '@cloudscape-design/components';
import { useAuth } from '../auth/AuthContext';
import { useTheme } from '../theme/ThemeContext';

export const TopNavigation: React.FC = () => {
  const { user, logout, isDevMode } = useAuth();
  const { mode, toggleMode } = useTheme();

  return (
    <CloudscapeTopNav
      identity={{ href: '/', title: 'AMF' }}
      utilities={[
        {
          type: 'button',
          iconName: mode === 'dark' ? 'star-filled' : 'star',
          title: `Switch to ${mode === 'dark' ? 'light' : 'dark'} mode`,
          onClick: toggleMode,
        },
        ...(isDevMode
          ? [{ type: 'button' as const, text: 'DEV MODE', disableTextCollapse: true }]
          : []),
        {
          type: 'menu-dropdown',
          text: user.email || 'User',
          description: user.roles.join(', '),
          items: [{ id: 'signout', text: 'Sign out' }],
          onItemClick: ({ detail }: { detail: { id: string } }) => {
            if (detail.id === 'signout') logout();
          },
        },
      ]}
    />
  );
};
