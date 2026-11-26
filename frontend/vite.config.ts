import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies /api to the backend. Point it at a cloud API by setting
// VITE_API_PROXY_TARGET (e.g. https://api.example.com) so the frontend can run
// locally while every other component runs on the cloud.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.VITE_API_PROXY_TARGET || 'http://localhost:8000';
  return {
    plugins: [react()],
    server: {
      port: 3000,
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          secure: true,
        },
        '/health': {
          target,
          changeOrigin: true,
          secure: true,
        },
      },
    },
  };
});
