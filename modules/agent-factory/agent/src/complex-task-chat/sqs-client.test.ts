import { deliveryRoutingForTask, TaskPayload } from './sqs-client';

describe('deliveryRoutingForTask', () => {
  it('carries the immutable tenant-qualified owner on every worker envelope', () => {
    const task: TaskPayload = {
      task_id: 'task-1',
      session_id: 'sess-1',
      message: 'hello',
      user_id: 'user-1',
      thread_id: 'thread-1',
      connection_id: 'conn-1',
      channel: 'webchat',
      platform_data: { requested_persona: 'developer' },
      owner_principal: '["org-1","org-1","team-1","user-1","webchat"]',
      session_generation: 1758441600,
    };

    expect(deliveryRoutingForTask(task)).toEqual({
      thread_id: 'thread-1',
      connection_id: 'conn-1',
      channel: 'webchat',
      channel_metadata: { requested_persona: 'developer' },
      owner_principal: '["org-1","org-1","team-1","user-1","webchat"]',
      session_generation: 1758441600,
    });
  });
});
