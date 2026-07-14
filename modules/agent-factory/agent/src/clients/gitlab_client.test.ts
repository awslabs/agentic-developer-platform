/**
 * Unit tests for GitLabClient.
 *
 * Issue #3325: Agent GitLabClient — Minimal Operations
 */

import { GitLabClient } from './gitlab_client';

// Mock global fetch
const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

describe('GitLabClient', () => {
  let client: GitLabClient;

  beforeEach(() => {
    client = new GitLabClient({
      baseUrl: 'https://gitlab.example.com',
      accessToken: 'glpat-test-token',
    });
    mockFetch.mockReset();
  });

  describe('constructor', () => {
    it('strips trailing slash from baseUrl', () => {
      const c = new GitLabClient({
        baseUrl: 'https://gitlab.example.com/',
        accessToken: 'tok',
      });
      // Verify by calling a method and checking the URL
      mockFetch.mockResolvedValueOnce({ ok: true });
      c.postIssueComment(1, 2, 'test');
      expect(mockFetch).toHaveBeenCalledWith(
        'https://gitlab.example.com/api/v4/projects/1/issues/2/notes',
        expect.anything(),
      );
    });
  });

  describe('postIssueComment', () => {
    it('posts a comment successfully', async () => {
      mockFetch.mockResolvedValueOnce({ ok: true, status: 201 });

      await client.postIssueComment(42, 7, 'Hello from agent');

      expect(mockFetch).toHaveBeenCalledWith(
        'https://gitlab.example.com/api/v4/projects/42/issues/7/notes',
        {
          method: 'POST',
          headers: {
            'PRIVATE-TOKEN': 'glpat-test-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ body: 'Hello from agent' }),
        },
      );
    });

    it('throws on 401 unauthorized', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 401,
        text: async () => '{"message":"401 Unauthorized"}',
      });

      await expect(client.postIssueComment(42, 7, 'test')).rejects.toThrow(
        /failed \(401\)/,
      );
    });

    it('throws on 404 not found', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 404,
        text: async () => '{"message":"404 Not Found"}',
      });

      await expect(client.postIssueComment(42, 999, 'test')).rejects.toThrow(
        /failed \(404\)/,
      );
    });
  });

  describe('createBranch', () => {
    it('creates a branch successfully', async () => {
      mockFetch.mockResolvedValueOnce({ ok: true, status: 201 });

      await client.createBranch(42, 'feature/agent-work', 'main');

      expect(mockFetch).toHaveBeenCalledWith(
        'https://gitlab.example.com/api/v4/projects/42/repository/branches',
        {
          method: 'POST',
          headers: {
            'PRIVATE-TOKEN': 'glpat-test-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ branch: 'feature/agent-work', ref: 'main' }),
        },
      );
    });

    it('throws on 409 branch already exists', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 409,
        text: async () => '{"message":"Branch already exists"}',
      });

      await expect(
        client.createBranch(42, 'existing-branch', 'main'),
      ).rejects.toThrow(/failed \(409\)/);
    });
  });

  describe('createMergeRequest', () => {
    it('creates a merge request and returns iid and web_url', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => ({
          iid: 15,
          web_url: 'https://gitlab.example.com/group/project/-/merge_requests/15',
          id: 100,
          title: 'Agent MR',
        }),
      });

      const result = await client.createMergeRequest(42, {
        sourceBranch: 'feature/agent-work',
        targetBranch: 'main',
        title: 'Agent MR',
        description: 'Automated changes',
      });

      expect(result).toEqual({
        iid: 15,
        web_url: 'https://gitlab.example.com/group/project/-/merge_requests/15',
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'https://gitlab.example.com/api/v4/projects/42/merge_requests',
        {
          method: 'POST',
          headers: {
            'PRIVATE-TOKEN': 'glpat-test-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            source_branch: 'feature/agent-work',
            target_branch: 'main',
            title: 'Agent MR',
            description: 'Automated changes',
          }),
        },
      );
    });

    it('sends undefined description when not provided', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => ({
          iid: 16,
          web_url: 'https://gitlab.example.com/group/project/-/merge_requests/16',
        }),
      });

      await client.createMergeRequest(42, {
        sourceBranch: 'fix/bug',
        targetBranch: 'main',
        title: 'Fix bug',
      });

      const callBody = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(callBody.source_branch).toBe('fix/bug');
      expect(callBody.target_branch).toBe('main');
      expect(callBody.title).toBe('Fix bug');
    });

    it('throws on failure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 422,
        text: async () => '{"message":["Source branch does not exist"]}',
      });

      await expect(
        client.createMergeRequest(42, {
          sourceBranch: 'nonexistent',
          targetBranch: 'main',
          title: 'Bad MR',
        }),
      ).rejects.toThrow(/failed \(422\)/);
    });
  });

  describe('getFile', () => {
    it('returns decoded file content', async () => {
      const content = 'Hello, World!\nLine 2\n';
      const base64Content = Buffer.from(content).toString('base64');

      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          file_name: 'README.md',
          file_path: 'README.md',
          content: base64Content,
          encoding: 'base64',
        }),
      });

      const result = await client.getFile(42, 'README.md', 'main');

      expect(result).toBe(content);
      expect(mockFetch).toHaveBeenCalledWith(
        'https://gitlab.example.com/api/v4/projects/42/repository/files/README.md?ref=main',
        {
          method: 'GET',
          headers: {
            'PRIVATE-TOKEN': 'glpat-test-token',
            'Content-Type': 'application/json',
          },
        },
      );
    });

    it('encodes file path with special characters', async () => {
      const base64Content = Buffer.from('data').toString('base64');
      mockFetch.mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ content: base64Content }),
      });

      await client.getFile(42, 'src/lib/utils.ts', 'develop');

      expect(mockFetch).toHaveBeenCalledWith(
        'https://gitlab.example.com/api/v4/projects/42/repository/files/src%2Flib%2Futils.ts?ref=develop',
        expect.anything(),
      );
    });

    it('throws on 404 file not found', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 404,
        text: async () => '{"message":"404 File Not Found"}',
      });

      await expect(
        client.getFile(42, 'nonexistent.txt', 'main'),
      ).rejects.toThrow(/failed \(404\)/);
    });
  });
});
