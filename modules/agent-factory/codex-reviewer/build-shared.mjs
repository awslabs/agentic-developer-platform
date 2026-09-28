/** Compile the single shared projection source with this adapter's pinned tsc.
 * No shared SDK dependency tree and no hand-maintained prompt/loader copy.
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve, dirname } from 'node:path';
const root = dirname(fileURLToPath(import.meta.url));
execFileSync(process.execPath, [resolve(root, 'node_modules/typescript/bin/tsc'),
  resolve(root, '../codex-harness/src/projection.ts'), '--outDir', resolve(root, '../codex-harness/dist'),
  '--target', 'ES2023', '--module', 'NodeNext', '--moduleResolution', 'NodeNext',
  '--strict', '--declaration', '--skipLibCheck', '--types', 'node',
  '--typeRoots', resolve(root, 'node_modules/@types')], { stdio: 'inherit' });
