import { ConfigLoader } from './ConfigLoader';

describe('ConfigLoader model assignment boundary (PMM-07)', () => {
  const original = process.env;

  beforeEach(() => {
    process.env = { ...original };
  });

  afterAll(() => {
    process.env = original;
  });

  it('never reassigns an entrypoint-resolved model', async () => {
    process.env.ANTHROPIC_MODEL = 'global.anthropic.claude-sonnet-4-6';
    const loader = new ConfigLoader();

    await loader.load();
    loader.setupBedrockEnv();

    expect(process.env.ANTHROPIC_MODEL).toBe(
      'global.anthropic.claude-sonnet-4-6',
    );
  });

  it('retains legacy initialization when no boundary value exists', async () => {
    delete process.env.ANTHROPIC_MODEL;
    const loader = new ConfigLoader();

    const config = await loader.load();

    expect(config.bedrockModel).toBe('global.anthropic.claude-sonnet-5');
    expect(process.env.ANTHROPIC_MODEL).toBe('global.anthropic.claude-sonnet-5');
  });
});
