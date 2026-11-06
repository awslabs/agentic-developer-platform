import { useEffect, useState } from 'react';
import { useLocalStorage } from '@/hooks/useLocalStorage';
import { apiClient } from '@/services/api';
import type { ChatExecutor, ChatSessionMode, ChatSessionState } from '@/services/chatMode';

/**
 * The runtime a conversation runs on (#8115). The choice is kept locally as the default for the
 * next conversation and, for an active conversation, sent to the gateway as the session's mode and
 * executor (`PUT /chat/sessions/{id}/mode`), so the persistent pod runs what the user picked.
 */
export type CrewRuntimeChoice = 'adp-assistant' | 'claude-code' | 'kirocrew-eks' | 'kirocrew-ec2';

export const RUNTIME_OPTIONS: ReadonlyArray<{ value: CrewRuntimeChoice; label: string; requires?: 'eks-pod' | 'ec2-instance' }> = [
  { value: 'adp-assistant', label: 'ADP assistant (quick answers)' },
  { value: 'claude-code', label: 'Claude Code in a sandbox (persistent, tools and workspace)' },
  { value: 'kirocrew-eks', label: 'Kiro Crew on EKS', requires: 'eks-pod' },
  { value: 'kirocrew-ec2', label: 'Kiro Crew on my EC2 box', requires: 'ec2-instance' },
];

export const EXECUTOR_LABELS: Record<ChatExecutor, string> = {
  'sandbox-loop': 'sandbox loop',
  'claude-code': 'Claude Code',
  kirocrew: 'Kiro Crew',
};

/** What a runtime choice means for the chat session on the gateway: the mode and, for persistent runtimes, the executor. */
export function sessionSelectionFor(choice: CrewRuntimeChoice): { mode: ChatSessionMode; executor?: ChatExecutor } {
  switch (choice) {
    case 'claude-code': return { mode: 'persistent', executor: 'claude-code' };
    case 'kirocrew-eks': return { mode: 'persistent', executor: 'kirocrew' };
    // The EC2 box is reached through its own dashboard (Open my EC2 Crew); chat turns stay on the assistant.
    default: return { mode: 'ephemeral' };
  }
}

/**
 * The select value for a conversation. The server-owned executor wins for a persistent (or pending
 * persistent) session; `persistent-default` names a persistent session that never chose one and so
 * runs the deployment default. Otherwise the local preference applies, but only its ephemeral choices.
 */
export function runtimeChoiceFor(
  state: Pick<ChatSessionState, 'mode' | 'executor' | 'pending_mode'> | null | undefined,
  stored: CrewRuntimeChoice,
): CrewRuntimeChoice | 'persistent-default' {
  if (state && (state.mode === 'persistent' || state.pending_mode === 'persistent')) {
    if (state.executor === 'claude-code') return 'claude-code';
    if (state.executor === 'kirocrew') return 'kirocrew-eks';
    return 'persistent-default';
  }
  return stored === 'adp-assistant' || stored === 'kirocrew-ec2' ? stored : 'adp-assistant';
}

export function useCrewRuntime() {
  const [storedChoice, setStoredChoice] = useLocalStorage<CrewRuntimeChoice>('adp_crew_runtime', 'adp-assistant');
  const [allowed, setAllowed] = useState(false);
  const [hostAllowed, setHostAllowed] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let current = true;
    apiClient.get<{ allowed_targets: string[] }>('/crew/runtime').then(result => {
      if (current) {
        const targets = Array.isArray(result.allowed_targets) ? result.allowed_targets : [];
        setAllowed(targets.includes('eks-pod'));
        setHostAllowed(targets.includes('ec2-instance'));
      }
    }).catch(() => { if (current) { setAllowed(false); setHostAllowed(false); } })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, []);

  const choice: CrewRuntimeChoice = storedChoice === 'claude-code' || (allowed && storedChoice === 'kirocrew-eks') || (hostAllowed && storedChoice === 'kirocrew-ec2')
    ? storedChoice : 'adp-assistant';
  const options = RUNTIME_OPTIONS.filter(option => !option.requires || (option.requires === 'eks-pod' ? allowed : hostAllowed));
  return { choice, setChoice: setStoredChoice, allowed, hostAllowed, loading, options };
}
