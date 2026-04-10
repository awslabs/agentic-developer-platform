import * as fs from 'fs/promises';
import { AgentState } from '../types';
import { Logger } from './Logger';
import { WorkspaceManager } from './WorkspaceManager';

export class StateManager {
  private logger: Logger;
  private workspaceManager: WorkspaceManager;

  constructor(logger: Logger, workspaceManager: WorkspaceManager) {
    this.logger = logger;
    this.workspaceManager = workspaceManager;
  }

  async saveState(state: AgentState): Promise<void> {
    const statePath = this.workspaceManager.getStatePath(state.workDir);
    await fs.writeFile(statePath, JSON.stringify(state, null, 2));
    this.logger.debug('State saved', { component: 'StateManager', phase: state.phase });
  }

  async loadState(workDir: string): Promise<AgentState | null> {
    const statePath = this.workspaceManager.getStatePath(workDir);
    try {
      const data = await fs.readFile(statePath, 'utf-8');
      return JSON.parse(data) as AgentState;
    } catch {
      return null;
    }
  }

  async clearState(workDir: string): Promise<void> {
    const statePath = this.workspaceManager.getStatePath(workDir);
    try {
      await fs.unlink(statePath);
    } catch {
      // Ignore if doesn't exist
    }
  }
}
