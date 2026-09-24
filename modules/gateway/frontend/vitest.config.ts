import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

// Superplane's onboarding interface lives in its own domain app
// (modules/domain-apps/superplane/ui) but is tested by this runner, because that
// directory has no node_modules and no toolchain of its own. Reaching outside
// this package root takes four coordinated settings; each is load-bearing, and
// the failure mode of omitting one is a suite that reports success while
// running none of those tests. test_superplane_registration.py pins all four.
const SUPERPLANE_UI = path.resolve(__dirname, '../../domain-apps/superplane/ui');

// Dependencies the domain app imports but cannot resolve on its own. Bare
// imports resolve by walking up from the importing file, and nothing above the
// domain app installs these, so each needs an explicit exact-match alias into
// this package's node_modules. react/react-dom must additionally resolve to a
// single copy or React's hook dispatcher sees two module instances and every
// render throws.
const BORROWED_DEPENDENCIES = [
  'msw',
  'react',
  'react-dom',
  '@testing-library/react',
  '@testing-library/user-event',
];

export default defineConfig({
  plugins: [react()],
  // Use development mode so React exports `act` for @testing-library/react.
  define: {
    'process.env.NODE_ENV': '"development"',
  },
  // Vite refuses to serve files outside its root, so without this the domain
  // app's tests are collected and then fail to import as `/@fs/...` not found.
  server: {
    fs: {
      allow: [SUPERPLANE_UI, path.resolve(__dirname)],
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: [
      'src/**/*.{test,spec}.{ts,tsx}',
      '../../domain-apps/superplane/ui/**/*.{test,spec}.{ts,tsx}',
    ],
    // `include` globs resolve against `root`; pinning it keeps the relative
    // path above stable no matter which directory vitest is invoked from.
    root: '.',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      exclude: [
        'node_modules/',
        'src/test/',
        'src/mocks/',
        '**/*.d.ts',
      ],
    },
  },
  resolve: {
    // Array form, and THE ORDER MATTERS. A string `find` matches as a prefix,
    // so a plain '@' entry placed first also swallows '@superplane-ui/...' and
    // '@testing-library/...', silently rewriting them to paths under src/ that
    // do not exist. '@superplane-ui' therefore precedes it, and '@' is a
    // regex anchored with a trailing slash so it can only match '@/'.
    alias: [
      { find: '@superplane-ui', replacement: SUPERPLANE_UI },
      { find: /^@\//, replacement: path.resolve(__dirname, './src') + '/' },
      ...BORROWED_DEPENDENCIES.map((dependency) => ({
        find: new RegExp(`^${dependency.replace(/[/-]/g, '\\$&')}$`),
        replacement: path.resolve(__dirname, 'node_modules', dependency),
      })),
    ],
  },
});
