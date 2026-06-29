/**
 * Shared GitHub posting utility with token refresh and S3 fallback.
 * All agent files should use this instead of raw gh CLI calls for posting.
 */
import * as fs from 'fs';
import { execSync } from 'child_process';

const REPO_OWNER = process.env.REPO_OWNER || '';
const REPO_NAME = process.env.REPO_NAME || '';

/**
 * Refresh the GitHub App installation token and update all env vars.
 */
export async function refreshGitHubToken(): Promise<void> {
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

    const resp = await fetch('https://api.github.com/app/installations', {
      headers: { Authorization: `Bearer ${jwtToken}`, Accept: 'application/vnd.github+json' },
    });
    const installations = await resp.json() as Array<{ id: number }>;
    if (!installations.length) return;

    const tokenResp = await fetch(
      `https://api.github.com/app/installations/${installations[0].id}/access_tokens`,
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
    }
  } catch {
    // Token refresh failed — will use existing token
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
