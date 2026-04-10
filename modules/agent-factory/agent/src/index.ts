import { AgentService } from './services/AgentService';

async function main(): Promise<void> {
  const agent = new AgentService();
  
  try {
    await agent.run();
    process.exit(0);
  } catch (err) {
    console.error('Fatal error:', err);
    process.exit(1);
  }
}

main();
