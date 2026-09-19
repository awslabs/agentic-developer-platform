jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));
import { query } from '@anthropic-ai/claude-agent-sdk';
import { PauseGate } from './pause-gate';
import { resilientQuery } from './utils/resilientQuery';

it('output admitted just before pause keeps confirmation pending until the consumer finishes', async () => {
  const gate = new PauseGate({ settleTimeoutMs: 5 });
  let pausing: ReturnType<PauseGate['requestPause']> | undefined;
  (query as jest.Mock).mockReturnValue(Object.assign((async function* () {
    yield { type: 'assistant', message: { content: [{ type: 'text', text: 'output' }] } };
  })(), { close: jest.fn() }));
  const stream = resilientQuery({
    queryParams: { prompt: 'task', options: {} },
    beforeOutput: () => {
      const admitted = gate.waitForOutput();
      pausing = gate.requestPause();
      return admitted;
    },
  });
  try {
    await stream.next();
    const pause = await pausing;
    expect(pause?.outcome).toBe('requested');
    expect(gate.currentPhase()).toBe('pause_requested');
  } finally { await gate.resume(); await stream.return(undefined as never); }
});

