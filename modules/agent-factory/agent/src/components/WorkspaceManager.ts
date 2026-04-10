import * as fs from 'fs/promises';
import * as path from 'path';
import * as os from 'os';
import { Logger } from './Logger';

export class WorkspaceManager {
  private baseDir = '/tmp/github-agent';
  private logger: Logger;

  constructor(logger: Logger) {
    this.logger = logger;
  }

  async createWorkspace(issueId: string, forceClean: boolean = false): Promise<string> {
    const workDir = path.join(this.baseDir, issueId);
    
    // If forceClean or directory exists, clean it first
    try {
      const exists = await fs.access(workDir).then(() => true).catch(() => false);
      if (exists) {
        this.logger.info(`Cleaning existing workspace: ${workDir}`, { component: 'WorkspaceManager' });
        await fs.rm(workDir, { recursive: true, force: true });
      }
    } catch (err) {
      // Ignore errors during cleanup check
    }
    
    await fs.mkdir(workDir, { recursive: true });
    this.logger.info(`Created workspace: ${workDir}`, { component: 'WorkspaceManager' });
    return workDir;
  }

  getWorkspacePath(issueId: string): string {
    return path.join(this.baseDir, issueId);
  }

  getRepoPath(workDir: string): string {
    return path.join(workDir, 'repo');
  }

  getStatePath(workDir: string): string {
    return path.join(workDir, 'state.json');
  }

  async cleanup(workDir: string): Promise<void> {
    try {
      await fs.rm(workDir, { recursive: true, force: true });
      this.logger.info(`Cleaned up workspace: ${workDir}`, { component: 'WorkspaceManager' });
    } catch (err) {
      this.logger.warn(`Failed to cleanup workspace: ${workDir}`, { component: 'WorkspaceManager' });
    }
  }

  async checkDiskSpace(): Promise<boolean> {
    const stats = await fs.statfs(os.tmpdir());
    const freeGB = (stats.bfree * stats.bsize) / (1024 * 1024 * 1024);
    const sufficient = freeGB > 5;
    if (!sufficient) {
      this.logger.warn(`Low disk space: ${freeGB.toFixed(2)}GB free`, { component: 'WorkspaceManager' });
    }
    return sufficient;
  }
}
