/** GitHub comments are input; only a current repository writer may authorize work. */
export async function hasRepositoryWritePermission(
  owner: string, repo: string, login: string, token: string,
): Promise<boolean> {
  if (!owner || !repo || !token || !/^[a-z\d](?:[a-z\d-]*[a-z\d])?$/i.test(login)) return false;
  try {
    const response = await fetch(
      `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/collaborators/${encodeURIComponent(login)}/permission`,
      { redirect: 'error', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' }, signal: AbortSignal.timeout(10_000) },
    );
    if (!response.ok) return false;
    const data = await response.json() as { permission?: string };
    return ['admin', 'maintain', 'write'].includes(data.permission ?? '');
  } catch {
    return false;
  }
}

/** Match an entire command and bind it to the particular posted plan. */
export function parsePlanApproval(body: string, requestId: string): { approved: boolean; feedback: string } | null {
  const match = body.trim().match(/^\/(approve|reject)\s+(\S+)(?:[ \t]+([^\r\n]+))?$/i);
  if (!match || match[2] !== requestId) return null;
  if (match[1].toLowerCase() === 'approve') return match[3] ? null : { approved: true, feedback: '' };
  return { approved: false, feedback: (match[3] ?? '').trim() };
}
