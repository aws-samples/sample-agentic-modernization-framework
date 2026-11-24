import React from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from './AuthContext';

interface Props {
  children: React.ReactNode;
  requiredRole?: string;
}

export const ProtectedRoute: React.FC<Props> = ({ children, requiredRole }) => {
  const { user, loading } = useAuth();

  // Wait for auth to initialize before redirecting
  if (loading) return <div style={{ padding: '2rem', textAlign: 'center' }}>Loading authentication...</div>;

  if (!user.isAuthenticated) return <Navigate to="/login" />;
  if (requiredRole && !user.roles.includes(requiredRole)) return <Navigate to="/unauthorized" />;
  return <>{children}</>;
};
