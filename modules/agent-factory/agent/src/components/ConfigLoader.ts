import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { Config } from '../types';

export class ConfigLoader {
  private config: Config | null = null;
  private secretsClient: SecretsManagerClient | null = null;

  async load(): Promise<Config> {
    if (this.config) return this.config;

    const awsRegion = process.env.AWS_REGION || 'us-east-1';
    
    this.config = {
      awsRegion,
      secretPrefix: process.env.SECRET_PREFIX || 'github-agent',
      pollingInterval: parseInt(process.env.POLLING_INTERVAL || '30000', 10),
      maxRetries: parseInt(process.env.MAX_RETRIES || '5', 10),
      logLevel: (process.env.LOG_LEVEL as Config['logLevel']) || 'INFO',
      bedrockModel: process.env.ANTHROPIC_MODEL || 'global.anthropic.claude-opus-5',
    };

    this.secretsClient = new SecretsManagerClient({ region: awsRegion });
    this.setupBedrockEnv();
    
    return this.config;
  }

  setupBedrockEnv(): void {
    process.env.CLAUDE_CODE_USE_BEDROCK = '1';
    if (this.config) {
      process.env.ANTHROPIC_MODEL = this.config.bedrockModel;
    }
  }

  async getSecret<T>(secretName: string): Promise<T> {
    if (!this.secretsClient || !this.config) {
      throw new Error('ConfigLoader not initialized');
    }

    const command = new GetSecretValueCommand({
      SecretId: `${this.config.secretPrefix}/${secretName}`,
    });

    const response = await this.secretsClient.send(command);
    if (!response.SecretString) {
      throw new Error(`Secret ${secretName} has no value`);
    }

    return JSON.parse(response.SecretString) as T;
  }

  getConfig(): Config {
    if (!this.config) throw new Error('ConfigLoader not initialized');
    return this.config;
  }
}
