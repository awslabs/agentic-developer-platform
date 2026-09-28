import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig(({ mode }) => ({
  // Release bundles receive public account settings from runtime-config.js.
  envPrefix: mode === 'release' ? '__ADP_NO_BUILD_SETTINGS__' : 'VITE_',
  plugins: [react()],
  base: '/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // Superplane's own interface lives with its domain app, not in this SPA.
      // The Gateway page mounts it and supplies the ADP session; the components
      // and their API client belong to the domain that owns the contract.
      //
      // Resolved through an alias rather than copied in, so there is exactly one
      // copy of the onboarding client and the browser and CLI cannot drift apart.
      '@superplane-ui': path.resolve(__dirname, '../../domain-apps/superplane/ui'),
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
}));
