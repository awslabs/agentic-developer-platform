import * as fs from 'fs/promises';
import * as path from 'path';
import { LockInfo } from '../types';
import { Logger } from './Logger';

const STALE_LOCK_MINUTES = 60; // Fallback for truly stale locks

export class ConcurrencyGuard {
  private lockDir = '/tmp/github-agent/locks';
  private logger: Logger;
  private currentIssueId: string | null = null;

  constructor(logger: Logger) {
    this.logger = logger;
  }

  private getLockFile(issueId: string): string {
    return path.join(this.lockDir, `${issueId}.lock`);
  }

  private isLockStale(lockInfo: LockInfo): boolean {
    const lockTime = new Date(lockInfo.startTime).getTime();
    const now = Date.now();
    const ageMinutes = (now - lockTime) / (1000 * 60);
    return ageMinutes > STALE_LOCK_MINUTES;
  }

  async acquireLock(issueId: string, forceOverride: boolean = false): Promise<boolean> {
    const lockFile = this.getLockFile(issueId);
    const lockInfo = await this.getLockInfo(issueId);
    
    if (lockInfo) {
      // Force override if this is a retry (forceOverride=true) or lock is stale
      if (forceOverride || this.isLockStale(lockInfo)) {
        this.logger.warn('Overriding existing lock', { 
          component: 'ConcurrencyGuard', 
          issueId,
          reason: forceOverride ? 'retry command' : 'stale lock',
          existingPid: lockInfo.pid,
          startTime: lockInfo.startTime
        });
        try {
          await fs.unlink(lockFile);
        } catch {
          // Ignore
        }
      } else {
        this.logger.warn('Lock already held for this issue', { 
          component: 'ConcurrencyGuard', 
          issueId,
          existingPid: lockInfo.pid,
          startTime: lockInfo.startTime
        });
        return false;
      }
    }

    await fs.mkdir(this.lockDir, { recursive: true });
    const info: LockInfo = { issueId, startTime: new Date().toISOString(), pid: process.pid };
    await fs.writeFile(lockFile, JSON.stringify(info));
    this.currentIssueId = issueId;
    this.logger.info('Lock acquired', { component: 'ConcurrencyGuard', issueId });
    return true;
  }

  async releaseLock(): Promise<void> {
    if (!this.currentIssueId) {
      return;
    }
    
    const lockFile = this.getLockFile(this.currentIssueId);
    try {
      await fs.unlink(lockFile);
      this.logger.info('Lock released', { component: 'ConcurrencyGuard', issueId: this.currentIssueId });
    } catch {
      // Ignore if doesn't exist
    }
    this.currentIssueId = null;
  }

  async isLocked(issueId: string): Promise<boolean> {
    return (await this.getLockInfo(issueId)) !== null;
  }

  async getLockInfo(issueId: string): Promise<LockInfo | null> {
    const lockFile = this.getLockFile(issueId);
    try {
      const data = await fs.readFile(lockFile, 'utf-8');
      return JSON.parse(data) as LockInfo;
    } catch {
      return null;
    }
  }
}
