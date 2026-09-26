/** Actual Codex runtime; shared repository tools carry the only edit authority. */
import { runCodexTask } from '../../../../tools/task-sdk/codex-runner.mjs';
import { repositoryToolDefinitions, codingModelStart } from './coding-driver.mjs';

export async function runCodexCoding(start, bridge, options = {}) {
  bridge.progress('Codex is inspecting the server-verified repository snapshot using bounded Task tools.', 'analysis');
  return runCodexTask(codingModelStart(start), bridge, repositoryToolDefinitions(start, bridge), options);
}
