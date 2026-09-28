import { describe, it, expect } from 'vitest';
import { taskRequest, downloadReport } from './task-client.js';

describe('direct Task API client', () => {
  it('uses the Cyber persona without overriding its model or budget', () => {
    const request = taskRequest('malware.wicar.org');
    expect(request.persona).toBe('agent-task-cyber');
    expect(request.inputs.url).toBe('https://malware.wicar.org');
    expect(request).not.toHaveProperty('model');
    expect(request).not.toHaveProperty('budget');
    for (const invalid of ['127.0.0.1', 'example.com/path', 'https://user:password@example.com', 'test.internal']) {
      expect(() => taskRequest(invalid)).toThrow();
    }
  });
  it('rejects tampered reports and keeps verified downloads inert', async () => {
    const html = '<h1>Report</h1><script>window.bad=true</script>';
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(html)))].map(b=>b.toString(16).padStart(2,'0')).join('');
    const calls: string[] = [];
    let digest = 'wrong';
    const fetcher = async (path: string) => {
      calls.push(path);
      return new Response(html, {headers: {'Content-Type':'text/html','X-Adp-Content-Sha256':digest}});
    };
    const snapshot = {result:{artifact_ids:['art_report']}};
    await expect(downloadReport(fetcher, 'tsk_example', snapshot)).rejects.toThrow('integrity');
    digest = hash;
    const blob = await downloadReport(fetcher, 'tsk_example', snapshot);
    const text = await new Promise<string>((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(blob); });
    expect(text).toContain("script-src 'none'");
    expect(calls).toEqual(['/v1/tasks/tsk_example/artifacts/art_report', '/v1/tasks/tsk_example/artifacts/art_report']);
  });
});
