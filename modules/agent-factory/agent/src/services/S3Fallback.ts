import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { Logger } from '../components/Logger';

const S3_BUCKET = process.env.AGENT_FALLBACK_BUCKET || 'adp-agent-state';
const S3_REGION = process.env.AWS_REGION || 'us-east-1';

export class S3Fallback {
  private s3: S3Client;
  private logger: Logger;
  private issueNumber: number;

  constructor(logger: Logger, issueNumber: number) {
    this.s3 = new S3Client({ region: S3_REGION });
    this.logger = logger;
    this.issueNumber = issueNumber;
  }

  /**
   * Upload data to S3 as a fallback when GitHub API calls fail.
   * Returns the S3 URI on success, or null if S3 also fails.
   */
  async upload(label: string, content: string): Promise<string | null> {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const key = `agent-fallback/issue-${this.issueNumber}/${timestamp}-${label}.md`;

    try {
      await this.s3.send(new PutObjectCommand({
        Bucket: S3_BUCKET,
        Key: key,
        Body: content,
        ContentType: 'text/markdown',
      }));
      const uri = `s3://${S3_BUCKET}/${key}`;
      this.logger.info('Fallback upload to S3 succeeded', { component: 'S3Fallback', uri });
      console.log(`📦 GitHub API failed — data saved to ${uri}`);
      return uri;
    } catch (err) {
      this.logger.error('S3 fallback also failed', err as Error, { component: 'S3Fallback', key });
      console.error(`❌ Both GitHub and S3 failed for ${label}`);
      return null;
    }
  }
}
