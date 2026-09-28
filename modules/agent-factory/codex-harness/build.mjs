/** Package shared Task contracts explicitly; no runtime imports into a checkout. */
import { copyFile, mkdir } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = dirname(fileURLToPath(import.meta.url));
const contracts = process.env.ADP_TASK_CONTRACTS_SOURCE || resolve(root, '../task-agents/investigator/dist');
const sdk = process.env.ADP_TASK_SDK_SOURCE || resolve(root, '../../tools/task-sdk');
await mkdir(resolve(root, 'dist/task-contracts'), { recursive: true });
await mkdir(resolve(root, 'dist/task-sdk'), { recursive: true });
for (const file of ['protocol.js', 'artifact-transfer.js']) await copyFile(resolve(contracts, file), resolve(root, 'dist/task-contracts', file));
await copyFile(resolve(sdk, 'protocol.mjs'), resolve(root, 'dist/task-sdk/protocol.mjs'));
await copyFile(resolve(root, 'src/task-entry.mjs'), resolve(root, 'dist/task-entry.mjs'));
