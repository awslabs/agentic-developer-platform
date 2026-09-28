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

/**
 * Defense-in-depth: persona name must match this pattern. Prevents path
 * traversal (e.g. `../../../etc/passwd`) if the SQS payload ever carries an
 * untrusted `agent_type`.
 */
const PERSONA_NAME_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

/**
 * Allowlist derived once at module load from the files baked into PERSONAS_DIR.
 * Keeps the surface area tight: only personas we've shipped are loadable.
 * Missing dir (e.g. during unit tests) falls back to an empty allowlist;
 * individual loads then use the DEFAULT_PERSONA.
 */
const ALLOWED_PERSONAS: ReadonlySet<string> = (() => {
  try {
    const entries = fs.readdirSync(PERSONAS_DIR);
    const names = entries
      .filter(f => f.endsWith('.md'))
      .map(f => f.replace(/\.md$/, ''))
      .filter(name => PERSONA_NAME_PATTERN.test(name));
    return new Set(names);
  } catch {
    return new Set<string>();
  }
})();

export interface ComposedPersona {
  name: string;
  baseSystemPrompt: string;
  learnings: MemoryRecord[];
  modelOverride?: string;
}

/**
 * Load a persona by name, composing base prompt from disk + learnings from memory.
 *
 * Validation: `name` must match PERSONA_NAME_PATTERN AND appear in the
 * baked-in allowlist. Anything else falls back to DEFAULT_PERSONA and logs a
 * warning (never throws — we want the turn to proceed with a safe default).
 */
export async function loadPersona(
  name: string,
  deps: { memory: MemoryProvider; query: string; tokenBudget: number },
): Promise<ComposedPersona> {
  const safeName = validatePersonaName(name);

  let baseSystemPrompt: string;
  if (safeName && ALLOWED_PERSONAS.has(safeName)) {
    const personaPath = path.join(PERSONAS_DIR, `${safeName}.md`);
    try {
      baseSystemPrompt = fs.readFileSync(personaPath, 'utf-8');
    } catch (err) {
      console.warn(
        `[persona-loader] Failed to read allowlisted persona ${safeName} at ${personaPath}: ${(err as Error).message}. Falling back to default.`,
      );
      baseSystemPrompt = DEFAULT_PERSONA;
    }
  } else {
    console.warn(
      `[persona-loader] Persona "${name}" not in allowlist (${Array.from(ALLOWED_PERSONAS).join(', ') || 'empty'}), using default`,
    );
    baseSystemPrompt = DEFAULT_PERSONA;
  }

  // Retrieve persona learnings via memory. Scope-key the RETRIEVAL using the
  // SAFE name so an attacker can't inject scope keys via persona.
  const scopedName = safeName ?? 'unknown';
  const learnings = await deps.memory.retrieve({
    query: deps.query,
    scope: { persona: scopedName },
    tokenBudget: deps.tokenBudget,
    kinds: ['learning'],
  });

  return { name: scopedName, baseSystemPrompt, learnings };
}

/**
 * Return the input if it's a safe persona name, or null otherwise.
 * Allowlist membership is checked separately in loadPersona.
 */
function validatePersonaName(name: unknown): string | null {
  if (typeof name !== 'string') return null;
  if (!PERSONA_NAME_PATTERN.test(name)) return null;
  return name;
}

/**
 * Compose the final system prompt from base persona, learnings, recalled experience,
 * and general memories.
 */
export function composeSystemPrompt(input: {
  base: string;
  personaLearnings: MemoryRecord[];
  memories: MemoryRecord[];
  /** Issue #1293: pre-formatted prior-experience section from recall-at-task-start hook. */
  priorExperience?: string;
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

  // Prior experience (recalled from personal-context; Issue #1293)
  if (input.priorExperience) {
    parts.push('');
    parts.push(input.priorExperience);
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

/** Exported for test setup only. */
export const __TEST_ONLY = { ALLOWED_PERSONAS, PERSONA_NAME_PATTERN };
