import React from 'react';
import { BrowserRouter, Routes, Route, useNavigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { ProtectedRoute } from './auth/ProtectedRoute';
import { ThemeProvider } from './theme/ThemeContext';
import { CapabilitiesProvider } from './CapabilitiesContext';
import { AppLayout } from './layout/AppLayout';
import Spinner from '@cloudscape-design/components/spinner';

// Route-level code splitting: each page is its own chunk, fetched on first visit.
const DashboardPage = React.lazy(() => import('./pages/DashboardPage').then((m) => ({ default: m.DashboardPage })));
const ProgramPage = React.lazy(() => import('./pages/ProgramPage').then((m) => ({ default: m.ProgramPage })));
const WorkflowPage = React.lazy(() => import('./pages/WorkflowPage').then((m) => ({ default: m.WorkflowPage })));
const ChatPage = React.lazy(() => import('./pages/ChatPage').then((m) => ({ default: m.ChatPage })));
const DiscoveryPage = React.lazy(() => import('./pages/DiscoveryPage').then((m) => ({ default: m.DiscoveryPage })));
const HowWeDoPage = React.lazy(() => import('./pages/HowWeDoPage').then((m) => ({ default: m.HowWeDoPage })));
const WavePlanningPage = React.lazy(() => import('./pages/WavePlanningPage').then((m) => ({ default: m.WavePlanningPage })));
const GovernancePage = React.lazy(() => import('./pages/GovernancePage').then((m) => ({ default: m.GovernancePage })));
const SettingsPage = React.lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));

const PageFallback = () => (
  <div style={{ padding: '2rem', textAlign: 'center' }}><Spinner size="large" /></div>
);

const Login = () => {
  const { login, user, loading } = useAuth();
  const navigate = useNavigate();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  // In dev mode login() authenticates in place without a redirect, and the
  // /login route always renders this component — so without an explicit
  // navigate the user stays here after a successful sign-in.
  React.useEffect(() => {
    if (!loading && user.isAuthenticated) {
      navigate('/', { replace: true });
    }
  }, [loading, user.isAuthenticated, navigate]);

  const handleSignIn = async () => {
    setBusy(true);
    setError(null);
    try {
      await login();
    } catch (e: any) {
      setError(e?.message || 'Sign-in failed. Check that the API is reachable.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ padding: '2rem', textAlign: 'center' }}>
      <h1>AMF</h1>
      <p style={{ color: '#687078', marginBottom: '1rem' }}>AI-powered large-scale migration and modernization</p>
      <button
        onClick={handleSignIn}
        disabled={busy}
        style={{ padding: '10px 24px', fontSize: 16, cursor: busy ? 'wait' : 'pointer', backgroundColor: busy ? '#9ba7b1' : '#0972d3', color: 'white', border: 'none', borderRadius: 4 }}
      >
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
      {error && <p style={{ color: '#d13212', marginTop: '1rem' }}>{error}</p>}
    </div>
  );
};

const App: React.FC = () => {
  return (
    <BrowserRouter>
      <ThemeProvider>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route
              path="/*"
              element={
                <ProtectedRoute>
                  <CapabilitiesProvider>
                  <AppLayout>
                    <React.Suspense fallback={<PageFallback />}>
                    <Routes>
                      <Route path="/" element={<DashboardPage />} />
                      <Route path="/program" element={<ProgramPage />} />
                      <Route path="/workflow" element={<WorkflowPage />} />
                      <Route path="/chat" element={<ChatPage />} />
                      <Route path="/inventory" element={<DiscoveryPage />} />
                      <Route path="/wave-planning" element={<WavePlanningPage />} />
                      <Route path="/governance" element={<GovernancePage />} />
                      <Route path="/how-we-do" element={<HowWeDoPage />} />
                      <Route path="/settings" element={<SettingsPage />} />
                    </Routes>
                    </React.Suspense>
                  </AppLayout>
                  </CapabilitiesProvider>
                </ProtectedRoute>
              }
            />
          </Routes>
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
};

export default App;
