/**
 * Persona loader — loads base persona from disk, retrieves learnings from memory.
 *
 * Base personas are baked into the Docker image at /app/personas/<type>.md.
 * Persona learnings are retrieved via MemoryProvider with scope.persona = name.
 */
import * as fs from 'fs';
import * as path from 'path';
import { MemoryProvider, MemoryRecord } from './memory/types';

const PERSONAS_DIR = process.env.PERSONAS_DIR ?? '/app/personas';
const DEFAULT_PERSONA = `You are a helpful assistant. Follow user instructions carefully and provide detailed, accurate responses.`;

export interface ComposedPersona {
  name: string;
  baseSystemPrompt: string;
  learnings: MemoryRecord[];
  modelOverride?: string;
}

/**
 * Load a persona by name, composing base prompt from disk + learnings from memory.
 */
export async function loadPersona(
  name: string,
  deps: { memory: MemoryProvider; query: string; tokenBudget: number },
): Promise<ComposedPersona> {
  // Load base persona from disk
  let baseSystemPrompt: string;
  const personaPath = path.join(PERSONAS_DIR, `${name}.md`);

  try {
    baseSystemPrompt = fs.readFileSync(personaPath, 'utf-8');
  } catch {
    console.warn(`[persona-loader] Persona file not found: ${personaPath}, using default`);
    baseSystemPrompt = DEFAULT_PERSONA;
  }

  // Retrieve persona learnings via memory
  const learnings = await deps.memory.retrieve({
    query: deps.query,
    scope: { persona: name },
    tokenBudget: deps.tokenBudget,
    kinds: ['learning'],
  });

  return { name, baseSystemPrompt, learnings };
}

/**
 * Compose the final system prompt from base persona, learnings, and general memories.
 */
export function composeSystemPrompt(input: {
  base: string;
  personaLearnings: MemoryRecord[];
  memories: MemoryRecord[];
}): string {
  const parts: string[] = [];

  // Base persona
  parts.push('<persona>');
  parts.push(input.base);
  parts.push('</persona>');

  // Persona learnings
  if (input.personaLearnings.length > 0) {
    parts.push('');
    parts.push('<persona-learnings>');
    for (const learning of input.personaLearnings) {
      const scope = Object.entries(learning.scope)
        .filter(([, v]) => v)
        .map(([k, v]) => `${k}:${v}`)
        .join(', ');
      parts.push(
        `  <memory id="${learning.id}" scope="${scope}" kind="${learning.kind ?? 'learning'}" updated="${learning.updatedAt ?? learning.createdAt}">`,
      );
      parts.push(`    ${learning.content}`);
      parts.push('  </memory>');
    }
    parts.push('</persona-learnings>');
  }

  // General memories (user prefs, component facts)
  if (input.memories.length > 0) {
    parts.push('');
    parts.push('<memories>');
    for (const mem of input.memories) {
      const scope = Object.entries(mem.scope)
        .filter(([, v]) => v)
        .map(([k, v]) => `${k}:${v}`)
        .join(', ');
      parts.push(
        `  <memory id="${mem.id}" scope="${scope}" kind="${mem.kind ?? 'fact'}" updated="${mem.updatedAt ?? mem.createdAt}">`,
      );
      parts.push(`    ${mem.content}`);
      parts.push('  </memory>');
    }
    parts.push('</memories>');
  }

  return parts.join('\n');
}
