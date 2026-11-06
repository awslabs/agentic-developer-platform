import * as fs from 'fs';
import * as path from 'path';

const SOURCES = [
  'agents/issue-authoring.md',
  'templates/epic-issue.md',
  'templates/developer-issue.md',
] as const;

/** Load a complete installed contract, never policy from an issue or model-supplied path. */
export function withIssueAuthoring(prompt: string, persona: 'PM' | 'AI-DLC', roots: readonly string[] = [
  path.resolve(__dirname, '../rules'), // /app/dist -> /app/rules in worker images
  path.resolve(__dirname, '../../rules'), // source checkout
]): string {
  const root = roots.find(candidate => SOURCES.every(source => fs.existsSync(path.join(candidate, source))));
  if (!root) throw new Error(`${persona} issue-authoring guide and templates are missing from the installation`);
  const sections = SOURCES.map(source => {
    const content = fs.readFileSync(path.join(root, source), 'utf8');
    if (!content.trim()) throw new Error(`${persona} issue-authoring source is empty: ${source}`);
    return `### ${source}\n${content}`;
  });
  return `## ${persona} epic/story authoring contract
Apply this contract when creating, refining, assigning or assessing epics and stories.
Use the epic template for epics and the developer template for stories. Replace
prompts before filing; preserve stable IDs, native parent links and explicit test
mappings. GitHub Issues remains the requirements source; local mirrors must not
replace its acceptance contract. Filing, format validity and passing tests do not
grant dispatch/deployment authority or independently establish acceptance.
For unrelated tasks, do not create issues merely because templates are supplied.
Existing AI-DLC approval gates and amendment restrictions still apply: these
templates do not authorize issue creation, plan acceptance or dispatch.

${sections.join('\n\n')}\n\n---\n\n${prompt}`;
}
