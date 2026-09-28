export * from '../../../../tools/task-sdk/protocol.mjs';
import { HostBridge as TaskBridge, ProtocolError } from '../../../../tools/task-sdk/protocol.mjs';
export const OPERATIONS = ['triage', 'static', 'result', 'url_analysis', 'dynamic', 'enrich', 'common_crawl_scan', 'common_crawl_result', 'common_crawl_read', 'browser_start', 'browser_step', 'browser_close', 'browser_inspect'];
export const CODE_OPERATIONS = ['start', 'execute', 'result', 'file', 'close'];
export const SKILLS = ['stage-1-triage', 'stage-2-osint', 'stage-3-static', 'stage-4-dynamic', 'stage-5-correlation', 'stage-6-verdict', 'stage-7-report', 'url-analysis'];
export class HostBridge extends TaskBridge {
  cyber(operation, payload) {
    if (!OPERATIONS.includes(operation)) throw new ProtocolError('Unsupported cyber tool');
    return this.tool('cyber.' + operation, payload);
  }
}
