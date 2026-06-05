import { Octokit } from '@octokit/rest';
import { execSync } from 'child_process';
import { TokenManager } from './TokenManager';
import { Logger } from './Logger';
import { S3Fallback } from '../services/S3Fallback';

export class GitHubClient {
  private tokenManager: TokenManager;
  private logger: Logger;
  private octokit: Octokit | null = null;
  private owner: string = '';
  private repo: string = '';
  private s3Fallback: S3Fallback | null = null;

  constructor(tokenManager: TokenManager, logger: Logger) {
    this.tokenManager = tokenManager;
    this.logger = logger;
  }

  async initialize(owner: string, repo: string, issueNumber?: number): Promise<void> {
    this.owner = owner;
    this.repo = repo;
    const token = await this.tokenManager.getToken();
    this.octokit = new Octokit({ auth: token });
    if (issueNumber) {
      this.s3Fallback = new S3Fallback(this.logger, issueNumber);
    }
    this.logger.info('GitHubClient initialized', { component: 'GitHubClient' });
  }

  async cloneRepo(workDir: string): Promise<void> {
    // Username-only URL — GIT_ASKPASS provides the password from $GITHUB_TOKEN
    const url = `https://x-access-token@github.com/${this.owner}/${this.repo}.git`;
    execSync(`git clone ${url} ${workDir}`, { stdio: 'pipe' });
    this.logger.info('Repository cloned', { component: 'GitHubClient' });
  }

  async createComment(issueNumber: number, body: string): Promise<number> {
    try {
      // Refresh Octokit token before the call
      const token = await this.tokenManager.getToken();
      this.octokit = new Octokit({ auth: token });

      const response = await this.octokit.issues.createComment({
        owner: this.owner,
        repo: this.repo,
        issue_number: issueNumber,
        body,
      });
      return response.data.id;
    } catch (err) {
      this.logger.warn('GitHub createComment failed, trying S3 fallback', {
        component: 'GitHubClient',
        error: (err as Error).message,
      });
      if (this.s3Fallback) {
        await this.s3Fallback.upload('comment', `# Issue #${issueNumber} Comment\n\n${body}`);
      }
      throw err;
    }
  }

  async updateComment(commentId: number, body: string): Promise<void> {
    await this.octokit!.issues.updateComment({
      owner: this.owner,
      repo: this.repo,
      comment_id: commentId,
      body,
    });
  }

  async getComments(issueNumber: number, since?: Date): Promise<{ id: number; body: string; created_at: string }[]> {
    const response = await this.octokit!.issues.listComments({
      owner: this.owner,
      repo: this.repo,
      issue_number: issueNumber,
      since: since?.toISOString(),
    });
    return response.data.map(c => ({ id: c.id, body: c.body || '', created_at: c.created_at }));
  }

  async createBranch(branchName: string, workDir: string): Promise<void> {
    const safeBranch = this.sanitizeBranchName(branchName);
    execSync(`git checkout -b ${safeBranch}`, { cwd: workDir, stdio: 'pipe' });
  }

  async commitAndPush(message: string, branchName: string, workDir: string): Promise<void> {
    const safeBranch = this.sanitizeBranchName(branchName);
    const safeMessage = message.replace(/"/g, '\\"').replace(/\$/g, '\\$').replace(/`/g, '\\`');
    
    // Configure git identity in the repo
    execSync('git config user.email "agent@github-ccsdk-agent.local"', { cwd: workDir, stdio: 'pipe' });
    execSync('git config user.name "GitHub CCSDK Agent"', { cwd: workDir, stdio: 'pipe' });
    
    // Stage all changes
    execSync('git add -A', { cwd: workDir, stdio: 'pipe' });
    
    // Check if there are any changes to commit
    try {
      const status = execSync('git status --porcelain', { cwd: workDir, encoding: 'utf-8' });
      this.logger.info('Git status before commit', { component: 'GitHubClient', status: status.trim() || '(empty)' });
      
      if (!status.trim()) {
        throw new Error('No files to commit. The code generation may not have produced any files.');
      }
    } catch (err) {
      if ((err as Error).message.includes('No files to commit')) {
        throw err;
      }
      // If git status fails, continue anyway
      this.logger.warn('Could not check git status', { component: 'GitHubClient', error: (err as Error).message });
    }
    
    // Commit changes
    try {
      execSync(`git commit -m "${safeMessage}"`, { cwd: workDir, stdio: 'pipe' });
    } catch (err) {
      const error = err as { stderr?: Buffer; stdout?: Buffer; message: string };
      const stderr = error.stderr?.toString() || '';
      const stdout = error.stdout?.toString() || '';
      this.logger.error('Git commit failed', new Error(error.message), { 
        component: 'GitHubClient', 
        stderr, 
        stdout 
      });
      throw new Error(`Git commit failed: ${stderr || stdout || error.message}`);
    }
    
    // Ensure token is refreshed before push — GIT_ASKPASS reads $GITHUB_TOKEN
    // from the environment at each git network call, so we just need the env
    // var to be current. tokenManager.getToken() handles the refresh.
    await this.tokenManager.getToken();

    // Push changes
    try {
      execSync(`git push origin ${safeBranch}`, { cwd: workDir, stdio: 'pipe' });
    } catch (err) {
      const error = err as { stderr?: Buffer; stdout?: Buffer; message: string };
      const stderr = error.stderr?.toString() || '';
      const stdout = error.stdout?.toString() || '';
      this.logger.error('Git push failed', new Error(error.message), { 
        component: 'GitHubClient', 
        stderr, 
        stdout 
      });
      throw new Error(`Git push failed: ${stderr || stdout || error.message}`);
    }
  }

  private sanitizeBranchName(name: string): string {
    return name.replace(/[^a-zA-Z0-9\-_\/]/g, '-');
  }

  async createPR(title: string, body: string, branchName: string, issueNumber: number): Promise<number> {
    const response = await this.octokit!.pulls.create({
      owner: this.owner,
      repo: this.repo,
      title,
      body: `${body}\n\nCloses #${issueNumber}`,
      head: branchName,
      base: 'main',
    });
    return response.data.number;
  }
}
