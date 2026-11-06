export function savedBoard() {
  return { state: 'available', path: '.adp/tasks/12.json', issue: 12, repository: 'example/service', repository_id: 7,
    branch: 'agent/issue-12', sha256: 'a'.repeat(64), confirmed_source_revision: 'b'.repeat(40),
    source: 'authorized_workspace', progress_only: true, board: { version: 2, issue: 12, tasks: [
      { id: 'AC-c1', kind: 'code', title: 'Existing code', status: 'done', note: 'Original completion evidence', planStep: { id: 'release', title: 'Release' } },
      { id: 'AC-t1', kind: 'test', title: 'Manual acceptance', status: 'blocked', note: 'Requires authorized acceptance', planStep: { id: 'release', title: 'Release' } },
      { id: 'AC-i1', kind: 'infra', title: 'Deploy', status: 'in_progress' },
      { id: 'AC-t2', kind: 'test', title: 'Verify', status: 'open' },
    ] } };
}
