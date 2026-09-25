import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
await mkdir('dist', { recursive: true });
await cp('src', 'dist', { recursive: true });
await cp(process.env.ADP_TASK_SDK_SOURCE || new URL('../../../tools/task-sdk/', import.meta.url), 'dist/task-runtime', { recursive: true });
for (const name of ['protocol.mjs', 'model-proxy.mjs', 'driver.mjs']) {
  const path = 'dist/' + name;
  await writeFile(path, (await readFile(path, 'utf8')).replaceAll('../../../../tools/task-sdk/', './task-runtime/'));
}
