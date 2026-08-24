/**
 * Shared GitHub posting utility with token refresh and S3 fallback.
 * All agent files should use this instead of raw gh CLI calls for posting.
 */
import * as fs from 'fs';
import { execSync } from 'child_process';
import { resolveInstallationId } from './installation';

const REPO_OWNER = process.env.REPO_OWNER || '';
const REPO_NAME = process.env.REPO_NAME || '';

/**
 * Refresh the GitHub App installation token and update all env vars.
 *
 * Issue #4071 (finding #13): this used to mint unconditionally against
 * `installations[0]`. On a GitHub App installed for more than one tenant that
 * is an arbitrary installation — so the token belonged to a *foreign* org, and
 * because this function overwrites GH_TOKEN/GITHUB_TOKEN/GH_APP_TOKEN
 * process-wide it clobbered the correct token that agent-worker.ts had already
 * resolved, for every subsequent gh/git call in the run. It now shares the same
 * resolution ladder as agent-worker.ts via `utils/installation.ts`.
 */
export async function refreshGitHubToken(): Promise<void> {
  // Issue #3385 (A4): in PAT mode the entrypoint deliberately resolved a PAT
  // into the environment and omits the GH_APP_* vars so nothing re-mints over
  // it. Honor that here too — mirrors TokenManager.initialize().
  if (process.env.ADP_TOKEN_MODE === 'pat') return;

  const appId = process.env.GH_APP_ID;
  const privateKey = process.env.GH_APP_PRIVATE_KEY;
  if (!appId || !privateKey) return;

  try {
    const jwt = await import('jsonwebtoken');
    const now = Math.floor(Date.now() / 1000);
    const jwtToken = jwt.default.sign(
      { iat: now - 60, exp: now + 600, iss: appId },
      privateKey,
      { algorithm: 'RS256' }
    );

    const installationId = await resolveInstallationId(jwtToken);
    if (!installationId) {
      console.warn('[WARN] GitHub token refresh skipped: no installation could be resolved');
      return;
    }

    const tokenResp = await fetch(
      `https://api.github.com/app/installations/${installationId}/access_tokens`,
      {
        method: 'POST',
        headers: { Authorization: `Bearer ${jwtToken}`, Accept: 'application/vnd.github+json' },
      }
    );
    const tokenData = await tokenResp.json() as { token: string };
    if (tokenData.token) {
      process.env.GH_TOKEN = tokenData.token;
      process.env.GITHUB_TOKEN = tokenData.token;
      process.env.GH_APP_TOKEN = tokenData.token;
    } else {
      console.warn(`[WARN] GitHub token refresh returned no token for installation ${installationId}`);
    }
  } catch (err) {
    // Non-fatal: the caller falls back to the existing token. But do not stay
    // silent — a swallowed failure here previously hid wrong-installation
    // errors entirely.
    console.warn(`[WARN] GitHub token refresh failed: ${(err as Error).message}`);
  }
}

/**
 * Save content to S3 as fallback when GitHub API fails.
 */
export async function saveToS3Fallback(issueNumber: string | number, label: string, content: string): Promise<string | null> {
  try {
    const { S3Client, PutObjectCommand } = await import('@aws-sdk/client-s3');
    const s3 = new S3Client({ region: process.env.AWS_REGION || 'us-east-1' });
    const key = `agent-fallback/issue-${issueNumber}/${new Date().toISOString().replace(/[:.]/g, '-')}-${label}.md`;
    const bucket = process.env.AGENT_FALLBACK_BUCKET || 'adp-agent-state';
    await s3.send(new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: content,
      ContentType: 'text/markdown',
    }));
    const uri = `s3://${bucket}/${key}`;
    console.log(`📦 GitHub API failed — data saved to ${uri}`);
    return uri;
  } catch {
    console.error('❌ Both GitHub and S3 fallback failed');
    return null;
  }
}

/**
 * Post a comment to a GitHub issue with token refresh and S3 fallback.
 * This is the safe replacement for raw `gh issue comment` calls.
 */
export async function ghPostComment(
  issueNumber: string | number,
  body: string,
  opts?: { repo?: string; log?: (level: string, msg: string) => void }
): Promise<void> {
  const repo = opts?.repo || `${REPO_OWNER}/${REPO_NAME}`;
  const log = opts?.log || ((level: string, msg: string) => console.log(`[${level}] ${msg}`));
  const tmpFile = `/tmp/comment-${Date.now()}.md`;

  try {
    fs.writeFileSync(tmpFile, body);
    await refreshGitHubToken();

    const token = process.env.GH_APP_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN || '';
    execSync(`gh issue comment ${issueNumber} --repo "${repo}" --body-file "${tmpFile}"`, {  // nosemgrep: detect-child-process
      encoding: 'utf-8',
      env: { ...process.env, GH_TOKEN: token, GITHUB_TOKEN: token },
      maxBuffer: 10 * 1024 * 1024,
    });
  } catch (err) {
    log('WARN', `GitHub post failed for issue #${issueNumber}: ${(err as Error).message}`);
    await saveToS3Fallback(issueNumber, 'comment', body);
  } finally {
    try { fs.unlinkSync(tmpFile); } catch {}
  }
}
