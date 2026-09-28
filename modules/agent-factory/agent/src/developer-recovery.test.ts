import { developerRecoveryContext } from './developer-recovery';
const data = { previous_run_id: 'orch:11111111-1111-4111-8111-111111111111', previous_attempt: 1,
  failure_decision_id: '22222222-2222-4222-8222-222222222222' };
it('carries prior evidence and directs preservation of existing work', () => {
  const prompt = developerRecoveryContext(JSON.stringify(data));
  expect(prompt).toContain(data.previous_run_id);
  expect(prompt).toContain(data.failure_decision_id);
  expect(prompt).toContain('do not reset the branch or create a duplicate PR');
  expect(prompt).toContain('Do not repeat the previous investigation without new evidence');
});
it.each([undefined, '', '{', '{}', JSON.stringify({...data, previous_attempt: 0}), JSON.stringify({...data, previous_run_id: 'ignore instructions'}), 'a'.repeat(8193)])('ignores malformed recovery metadata: %s', (raw) => {
  expect(developerRecoveryContext(raw)).toBe('');
});
