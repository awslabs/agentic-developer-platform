import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
await mkdir('dist', { recursive: true });
await cp('src', 'dist', { recursive: true });
await cp(process.env.ADP_TASK_SDK_SOURCE || new URL('../../../tools/task-sdk/', import.meta.url), 'dist/task-runtime', { recursive: true });
for (const name of ['index.js', 'protocol.mjs', 'model-proxy.mjs', 'driver.mjs', 'coding-driver.mjs', 'codex-driver.mjs']) {
  const path = 'dist/' + name;
  await writeFile(path, (await readFile(path, 'utf8')).replaceAll('../../../../tools/task-sdk/', './task-runtime/'));
}
// The base platform image packages only its shared developer drivers. Cyber's
// hosted-worker overlay adds the Cyber driver from this module's build output.
if (process.env.ADP_TASK_INCLUDE_CYBER === 'false') {
  await rm('dist/driver.mjs');
  await rm('dist/archive-progress.mjs');
}
