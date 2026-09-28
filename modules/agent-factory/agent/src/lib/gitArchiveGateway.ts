/** Preserve git recovery archives through bounded own-run uploads. */
import { createHash } from 'node:crypto';
import { open, unlink } from 'node:fs/promises';
import { MAX_ARTIFACT_BYTES, uploadRunArtifact } from './artifactGateway';

export async function archiveProtectedGitChanges(options: {
  archivePath: string;
  issueNumber: string | number;
  timestamp: string;
  files: string[];
}): Promise<boolean> {
  const { archivePath, issueNumber, timestamp, files } = options;
  const parts: { uri: string; bytes: number; sha256: string }[] = [];
  try {
    const file = await open(archivePath, 'r');
    const digest = createHash('sha256');
    let bytes = 0;
    try {
      // The tar is complete before this function starts. Read and upload one
      // bounded part at a time; the gateway's request limit is not an archive limit.
      const size = (await file.stat()).size;
      if (!size) throw new Error('Empty archive');
      while (bytes < size) {
        const buffer = Buffer.alloc(Math.min(MAX_ARTIFACT_BYTES, size - bytes));
        const { bytesRead } = await file.read(buffer, 0, buffer.length, bytes);
        if (!bytesRead) throw new Error('Incomplete archive');
        const body = buffer.subarray(0, bytesRead);
        const uploaded = await uploadRunArtifact('git-changes', body);
        digest.update(body);
        bytes += bytesRead;
        parts.push({ uri: uploaded.uri, bytes: bytesRead, sha256: createHash('sha256').update(body).digest('hex') });
        // Keep recoverable locations visible even if a later part/manifest fails.
        console.log(`📦 Git recovery part ${parts.length}: ${uploaded.uri} (${bytesRead} bytes)`);
      }
    } finally { await file.close(); }
    const manifest = [
      '# Git Changes Backup', `Issue: #${issueNumber}`, `Timestamp: ${timestamp}`,
      `Archive bytes: ${bytes}`, `Archive SHA256: ${digest.digest('hex')}`, '',
      'Download the parts below and concatenate their bytes in this exact order to recover the tar.gz archive.',
      'Repeated URIs represent repeated bytes and must be included each time.', '',
      ...parts.map((part, index) => `${index + 1}. ${part.uri} (${part.bytes} bytes; SHA256 ${part.sha256})`),
      '', 'Files:', ...files.map(name => '- ' + name), '',
    ].join('\n');
    const receipt = await uploadRunArtifact('git-manifest', manifest);
    console.log(`📦 Git push failed — ${files.length} changed files saved; recovery manifest: ${receipt.uri}`);
    await unlink(archivePath).catch(() => undefined);
    return true;
  } catch {
    // Never surface transport errors containing signed headers. A warning in
    // an internal logger alone is insufficient for irreplaceable run output.
    console.error(`❌ Git push failed and own-run archival failed — ${files.length} changed files could NOT be preserved as a complete backup. Local archive retained at ${archivePath}.`);
    return false;
  }
}
