import { protectedArtifactRun, uploadRunArtifact } from '../lib/artifactGateway';
import { workerAwsCredentials, workerAwsRegion } from '../lib/runIdentity';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { Logger } from '../components/Logger';
import { resolveFallbackBucket, buildFallbackKey } from '../utils/s3Fallback';

const S3_REGION = workerAwsRegion();

export class S3Fallback {
  private s3: S3Client;
  private logger: Logger;
  private issueNumber: number;

  constructor(logger: Logger, issueNumber: number) {
    this.s3 = new S3Client({ region: S3_REGION, credentials: workerAwsCredentials() });
    this.logger = logger;
    this.issueNumber = issueNumber;
  }

  /**
   * Upload data to S3 as a fallback when GitHub API calls fail.
   * Returns the S3 URI on success, or null if S3 also fails.
   */
  async upload(label: string, content: string): Promise<string | null> {
    if (protectedArtifactRun()) {
      try { return (await uploadRunArtifact('comment', content)).uri; }
      catch { this.logger.error('Own-run artifact archive unavailable', undefined, { component: 'S3Fallback' }); return null; }
    }
    // Issue #4184: no hardcoded bucket default — resolve from config or skip.
    const bucket = resolveFallbackBucket(msg =>
      this.logger.error(msg, undefined, { component: 'S3Fallback' })
    );
    if (!bucket) return null;

    const key = buildFallbackKey(this.issueNumber, label);

    try {
      await this.s3.send(new PutObjectCommand({
        Bucket: bucket,
        Key: key,
        Body: content,
        ContentType: 'text/markdown',
      }));
      const uri = `s3://${bucket}/${key}`;
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
