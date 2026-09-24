// AWS acceptance through the same Claude Agent SDK installed in the hosted image.
// This does not invoke GitHub ingress, post messages, or replace production orchestration.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { readFileSync, appendFileSync } from 'node:fs';

const task = JSON.parse(readFileSync(0, 'utf8'));
const require = createRequire('/app/package.json');
const { query } = await import(pathToFileURL(require.resolve('@anthropic-ai/claude-agent-sdk')).href);
const abortController = new AbortController();
const timer = setTimeout(() => abortController.abort(), 250_000);
const session = query({
  prompt: task.prompt,
  options: {
    cwd: task.cwd,
    model: task.model,
    systemPrompt: task.instructions,
    allowedTools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Skill'],
    tools: ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Skill'],
    settingSources: [],
    permissionMode: 'bypassPermissions',
    allowDangerouslySkipPermissions: true,
    maxTurns: 40,
    persistSession: false,
    abortController,
  },
});
try {
  for await (const message of session) {
    // Full SDK output stays in the AWS workspace and S3, never normal pod logs.
    if (Array.isArray(message.message?.content)) {
      message.message.content = message.message.content.filter(block =>
        !['thinking', 'redacted_thinking'].includes(block.type));
    }
    appendFileSync(task.transcript, JSON.stringify(message) + '\n');
    if (message.type === 'result') {
      clearTimeout(timer);
      if (message.is_error || message.subtype !== 'success') {
        throw new Error(`Hosted SDK result: ${message.subtype}`);
      }
      break;
    }
  }
} catch (error) {
  // Avoid Node printing a whole minified SDK source line before the useful error.
  process.stderr.write(JSON.stringify({ error: error.name, message: String(error.message).slice(0, 2000) }) + '\n');
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  session.close();
}
