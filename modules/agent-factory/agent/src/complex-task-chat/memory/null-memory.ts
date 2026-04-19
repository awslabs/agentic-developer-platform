/**
 * NullMemoryProvider — no-op implementation. Default when memory is disabled.
 *
 * Returns empty results, exposes no tools, all capabilities false.
 */
import { MemoryProvider, MemoryRecord, MemoryQuery, MemoryCapabilities } from './types';
import { AgentTool } from '../context/types';

export class NullMemoryProvider implements MemoryProvider {
  async retrieve(_input: MemoryQuery): Promise<MemoryRecord[]> {
    return [];
  }

  async save(record: Omit<MemoryRecord, 'id' | 'createdAt'>): Promise<MemoryRecord> {
    return {
      ...record,
      id: `mem_null_${Date.now()}`,
      createdAt: new Date().toISOString(),
    };
  }

  async delete(_id: string): Promise<void> {
    // no-op
  }

  tools(): AgentTool[] {
    return [];
  }

  capabilities(): MemoryCapabilities {
    return {
      semanticSearch: false,
      keywordSearch: false,
      tagFiltering: false,
      scoping: [],
      delete: false,
      asyncExtraction: false,
      ttl: false,
    };
  }
}
