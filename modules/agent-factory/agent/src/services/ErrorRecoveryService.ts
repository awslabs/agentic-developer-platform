import { Logger } from '../components/Logger';
import { Config, AgentState, RetryableError } from '../types';

export class ErrorRecoveryService {
  private logger: Logger;
  private config: Config;
  private errorCounts: Map<string, number> = new Map();

  constructor(logger: Logger, config: Config) {
    this.logger = logger;
    this.config = config;
  }

  async handleError(error: Error, state: AgentState): Promise<boolean> {
    const errorKey = this.getErrorKey(error);
    const count = (this.errorCounts.get(errorKey) || 0) + 1;
    this.errorCounts.set(errorKey, count);

    this.logger.error('Error occurred', error, { 
      component: 'ErrorRecoveryService', 
      errorKey, 
      count,
      phase: state.phase 
    });

    // Loop detection
    if (count > this.config.maxRetries) {
      this.logger.error('Max retries exceeded, breaking loop', error, { component: 'ErrorRecoveryService' });
      return false;
    }

    // Check if retryable
    if (this.isRetryable(error)) {
      const delay = this.getBackoffDelay(count);
      this.logger.info(`Retrying in ${delay}ms`, { component: 'ErrorRecoveryService', attempt: count });
      await this.sleep(delay);
      return true;
    }

    return false;
  }

  private isRetryable(error: Error): boolean {
    const retryableError = error as RetryableError;
    if (retryableError.retryable !== undefined) return retryableError.retryable;

    const message = error.message.toLowerCase();
    const retryablePatterns = [
      'timeout', 'econnreset', 'rate limit', '429', '503', '502',
      'econnrefused', 'socket hang up', 'epipe', 'enotfound',
      'network', 'aborted', 'sigint', 'signal', 'cancelled',
      'throttl', 'too many requests', 'service unavailable',
      'fetch failed', 'overloaded', 'bad gateway', 'gateway timeout',
      'rate_limit', 'capacity',
    ];
    return retryablePatterns.some(p => message.includes(p));
  }

  private getErrorKey(error: Error): string {
    return `${error.name}:${error.message.slice(0, 50)}`;
  }

  private getBackoffDelay(attempt: number): number {
    const exponential = 1000 * Math.pow(2, attempt - 1);
    const jitter = Math.random() * 2000;
    return Math.min(exponential + jitter, 60_000);
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  resetErrorCounts(): void {
    this.errorCounts.clear();
  }
}
