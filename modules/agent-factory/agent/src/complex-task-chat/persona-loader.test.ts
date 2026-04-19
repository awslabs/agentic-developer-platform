import { composeSystemPrompt } from './persona-loader';

describe('composeSystemPrompt', () => {
  it('renders base persona only', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [],
      memories: [],
    });
    expect(result).toContain('<persona>');
    expect(result).toContain('You are a developer.');
    expect(result).toContain('</persona>');
    expect(result).not.toContain('<persona-learnings>');
    expect(result).not.toContain('<memories>');
  });

  it('renders persona learnings block', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [
        {
          id: 'mem_1',
          content: 'EKS cluster name is adp-dev-eks-cluster',
          scope: { persona: 'developer' },
          kind: 'learning',
          createdAt: '2026-04-10T00:00:00Z',
        },
      ],
      memories: [],
    });
    expect(result).toContain('<persona-learnings>');
    expect(result).toContain('mem_1');
    expect(result).toContain('EKS cluster name is adp-dev-eks-cluster');
    expect(result).toContain('</persona-learnings>');
  });

  it('renders memories block', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [],
      memories: [
        {
          id: 'mem_2',
          content: 'Prefers real-time updates',
          scope: { user: 'pranav' },
          kind: 'preference',
          createdAt: '2026-04-14T00:00:00Z',
        },
      ],
    });
    expect(result).toContain('<memories>');
    expect(result).toContain('mem_2');
    expect(result).toContain('Prefers real-time updates');
    expect(result).toContain('</memories>');
  });

  it('renders all blocks together', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [
        {
          id: 'mem_1',
          content: 'learning content',
          scope: { persona: 'dev' },
          kind: 'learning',
          createdAt: '2026-04-10T00:00:00Z',
        },
      ],
      memories: [
        {
          id: 'mem_2',
          content: 'memory content',
          scope: { user: 'test' },
          kind: 'fact',
          createdAt: '2026-04-10T00:00:00Z',
        },
      ],
    });
    expect(result).toContain('<persona>');
    expect(result).toContain('<persona-learnings>');
    expect(result).toContain('<memories>');
  });
});
