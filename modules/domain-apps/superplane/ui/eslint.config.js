// Lint configuration for Superplane's own onboarding interface.
//
// WHY THIS FILE EXISTS RATHER THAN A PATH ADDED TO THE GATEWAY'S CONFIG
// --------------------------------------------------------------------
// ESLint's flat config resolves rules relative to the config file's own
// directory and refuses files outside that base path: passing this directory to
// the Gateway frontend's `eslint .` reports "all of the files matching the glob
// are ignored" and exits non-zero without linting anything. That failure mode is
// the dangerous one — it looks like a lint error while checking no code at all.
//
// So the domain app carries its own config, matching the Gateway frontend's
// rules so one directory is not held to a laxer standard than the other. It is
// invoked by the frontend's `lint` script, which runs eslint here as a second
// pass; `tests/features/test_superplane_registration.py` pins that wiring so the
// two cannot drift apart silently.
//
// The imports below are resolved from the Gateway frontend's installed packages
// through a file URL, because this directory has no node_modules of its own and
// flat config offers no equivalent of the old `--resolve-plugins-relative-to`.
// Installing a second eslint/typescript-eslint here would let the two directories
// drift onto different rule versions, which is exactly what this config exists to
// prevent.
import { createRequire } from 'node:module';

const fromFrontend = createRequire(
  new URL('../../../gateway/frontend/package.json', import.meta.url),
);
const globals = fromFrontend('globals');
const tseslint = fromFrontend('typescript-eslint');

export default [
  { ignores: ['dist', 'node_modules', '**/*.d.ts'] },
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
];
