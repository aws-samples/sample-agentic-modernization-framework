import React from 'react';
import { AppLayout as CloudscapeAppLayout } from '@cloudscape-design/components';
import { TopNavigation } from './TopNavigation';
import { SideNavigation } from './SideNavigation';

interface Props {
  children: React.ReactNode;
}

export const AppLayout: React.FC<Props> = ({ children }) => {
  return (
    <>
      <TopNavigation />
      <CloudscapeAppLayout
        navigation={<SideNavigation />}
        content={children}
        toolsHide={true}
      />
    </>
  );
};
